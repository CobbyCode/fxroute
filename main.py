# SPDX-License-Identifier: AGPL-3.0-only

"""Main FastAPI application for FXRoute."""

import copy
import cmath
import json
import logging
import math
import os
import re
import shutil
import time
import weakref
import asyncio
import hashlib
import inspect
import subprocess
import playback.queue as playback_queue
import playback.media_readiness as media_readiness
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote, unquote, urlparse

import uvicorn
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

from config import get_settings
from http_errors import bad_request, internal_error
from http_origin import (
    TrustedOriginMiddleware,
    effective_request_scheme,
    is_request_origin_trusted,
)
from connection_manager import ConnectionManager
from common.atomic_write import atomic_write_bytes
import system_update as update_lifecycle
from library.sources import MusicLibraryManager
from radio.metadata import RadioMetadataService
from safe_http import BlockedUrlError

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
COVER_CACHE_DIR = BASE_DIR / "media" / "cache" / "covers"
UPDATE_SCRIPT = BASE_DIR / "scripts" / "update_fxroute.sh"
# Same rule as install.sh valid_local_hostname().
_LOCAL_HOSTNAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_LOCAL_HOSTNAME_RESERVED = {"localhost"}

# Cooldown to prevent rapid mpv IPC flooding (ms)
PLAY_COMMAND_COOLDOWN_MS = 400
# Bounded window for the idempotent MPV->DSP ingress link reconciliation
# after the source ports appeared (link creation plus readback confirm).
MPV_LINK_REPAIR_TIMEOUT_MS = 1500
PEAK_MONITOR_RESTART_SETTLE_MS = 320
# Bounded readback wait for the native DSP ports after a rate switch or a
# missing-graph repair. No fixed sleeps: the handoff polls pw-link until the
# fxroute_dsp input/output ports are exposed, then starts/syncs the helper.
PLAYBACK_HANDOFF_DSP_PORT_TIMEOUT_MS = 5000
# A post-source-start graph repair is deliberately a short, deterministic
# readback window.  It is not a second watcher or a general graph recovery.
POST_START_GRAPH_STABILITY_READBACKS = 2
SPOTIFY_STATE_POLL_INTERVAL_SECONDS = 2.0
SPOTIFY_STATE_IDLE_POLL_INTERVAL_SECONDS = 5.0
SPOTIFY_STATE_REFRESH_DEBOUNCE_SECONDS = 0.20
MEASUREMENT_WINDOW_TTL_SECONDS = 30.0

# Track last play command time to debounce rapid requests
_last_play_command_time = 0.0


def _can_send_play_command():
    """Debounce rapid play/pause/seek commands to prevent mpv IPC overload."""
    global _last_play_command_time
    now = time.monotonic()
    if now - _last_play_command_time < PLAY_COMMAND_COOLDOWN_MS / 1000:
        return False
    _last_play_command_time = now
    return True


def _read_version_file() -> str:
    """Thin wrapper: VERSION reading lives in install_info (REFACTOR-009)."""
    return install_info.read_version_file()


def is_live_mode() -> bool:
    """Return True inside the volatile Try FXRoute live session.

    Detection is intentionally redundant: the live root ships the marker
    file /etc/fxroute-live, and the live GRUB entry passes fxroute.live=1
    on the kernel cmdline. Either signal counts; both are absent on
    installed systems. Read-only, no caching (tests patch Path/cmdline).
    """
    try:
        if Path("/etc/fxroute-live").is_file():
            return True
    except OSError:
        pass
    try:
        cmdline = Path("/proc/cmdline").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "fxroute.live=1" in cmdline.split()


def _read_build_id() -> str:
    """Thin wrapper: build-id resolution lives in install_info (REFACTOR-009)."""
    return install_info.read_build_id()


def _configured_service_name() -> str:
    """Thin wrapper: service-name resolution lives in install_info (REFACTOR-009)."""
    return install_info.configured_service_name()


_UPDATE_CHECK_TIMEOUT_SECONDS = 90
_UPDATE_APPLY_TIMEOUT_SECONDS = 15 * 60
_UPDATE_TERMINATE_GRACE_SECONDS = 5
_SERVICE_RESTART_TIMEOUT_SECONDS = 15
_SERVICE_RESTART_TERMINATE_GRACE_SECONDS = 3


def _make_system_update_deps() -> update_lifecycle.SystemUpdateDeps:
    """Bind the update orchestration to the application services.

    Resolved at call time, so tests that patch main attributes (UPDATE_SCRIPT,
    timeout constants, logger) observe the patched values through the thin
    wrappers below (same contract as the ``configure_*`` pattern used by the
    extracted routers).
    """
    return update_lifecycle.SystemUpdateDeps(
        stop_process_group=pw_link.stop_process_group_cancellation_safe,
        stop_command_child=pw_link.stop_command_child_cancellation_safe,
        log_warning=logger.warning,
    )


async def _run_update_script(timeout: float, *args: str) -> dict:
    """Thin wrapper: update-script lifecycle lives in system_update (REFACTOR-012)."""
    return await update_lifecycle.run_update_script(
        UPDATE_SCRIPT,
        timeout,
        *args,
        terminate_grace_seconds=_UPDATE_TERMINATE_GRACE_SECONDS,
        deps=_make_system_update_deps(),
    )


def _get_update_operation_lock() -> asyncio.Lock:
    """Thin wrapper: the exclusive update guard lives in system_update (REFACTOR-012)."""
    return update_lifecycle.get_update_operation_lock()


async def _run_update_operation(timeout: float, *args: str, on_result=None) -> dict:
    """Thin wrapper: exclusive update guard lives in system_update (REFACTOR-012)."""
    return await update_lifecycle.run_update_operation(
        timeout,
        *args,
        script_path=UPDATE_SCRIPT,
        terminate_grace_seconds=_UPDATE_TERMINATE_GRACE_SECONDS,
        deps=_make_system_update_deps(),
        on_result=on_result,
    )


async def _restart_fxroute_service_after_response(service_name: str) -> None:
    """Thin wrapper: deferred service restart lives in system_update (REFACTOR-012)."""
    try:
        await update_lifecycle.restart_service_after_response(
            service_name,
            restart_timeout_seconds=_SERVICE_RESTART_TIMEOUT_SECONDS,
            restart_terminate_grace_seconds=_SERVICE_RESTART_TERMINATE_GRACE_SECONDS,
            deps=_make_system_update_deps(),
        )
    finally:
        update_lifecycle.finish_deferred_restart()


def _measurement_blocks_playback_rate(expected_rate: Optional[int]) -> Optional[int]:
    """Resolve the session-owned playback-rate block decision for samplerate deps.

    The active-and-jobs decision lives on ``MeasurementSampleRateSession``
    (``blocks_playback_rate``); this is only the injection bridge that guards
    the not-yet-created session and delegates to the owner.
    """
    if measurement_sr_session is None:
        return None
    return measurement_sr_session.blocks_playback_rate(expected_rate)


def _measurement_session_owns_live_rate() -> bool:
    """Return whether an open measurement window owns the live sample rate.

    Idle playback paths must not clear the force-rate pin then: the sweep runs
    at the measurement rate, and the session release is the owner that
    restores or clears the pin.
    """
    session = measurement_sr_session
    return session is not None and session.owns_audio_graph


def _is_local_playback_active(state: dict | None) -> bool:
    return playback_state_helpers.is_local_playback_active(state)

def _is_spotify_playback_active(state: dict | None) -> bool:
    return playback_state_helpers.is_spotify_playback_active(state)


def _is_measurement_window_open() -> bool:
    if last_measurement_window_seen_at <= 0:
        return False
    return (time.monotonic() - last_measurement_window_seen_at) <= MEASUREMENT_WINDOW_TTL_SECONDS


def _active_line_source_for_power() -> str | None:
    """Routed line source keeping the amp hint on, if any.

    Bluetooth counts only while its linked capture source shows live
    WirePlumber stream links (merely connected or paused stays off);
    external input counts while its loopback link is established. Both
    link names are owned by the routing singletons; the Bluetooth case
    costs one local ``wpctl`` read.
    """
    try:
        bluetooth_source = (bluetooth_input.input_source_name or "").strip()
    except Exception:
        bluetooth_source = ""
    if bluetooth_source and is_bluetooth_audio_streaming(bluetooth_source):
        return "bluetooth"
    try:
        external_source = (external_input.loopback_source_name or "").strip()
    except Exception:
        external_source = ""
    if external_source:
        return "external-input"
    return None


def _build_power_state_payload() -> dict:
    local_state = runtime.player_instance.state if runtime.player_instance else {}
    spotify_state = playback_state.latest_spotify_state or {}
    qobuz_state = playback_state.latest_qobuz_state or {}
    playback_active = (
        _is_local_playback_active(local_state)
        or _is_spotify_playback_active(spotify_state)
        or _is_qobuz_playback_active(qobuz_state)
    )
    measurement_window_open = _is_measurement_window_open()
    # Measurement keeps priority and skips the line-source probe entirely;
    # its behavior is unchanged by the Bluetooth/external extension below.
    line_source = None if measurement_window_open else _active_line_source_for_power()
    if measurement_window_open:
        reason = "measurement_window"
    elif playback_active:
        reason = "playback"
    elif line_source == "bluetooth":
        reason = "bluetooth"
    elif line_source == "external-input":
        reason = "external-input"
    else:
        reason = "idle"
    return {
        "amp_should_be_on": bool(playback_active or measurement_window_open or line_source),
        "reason": reason,
        "playback_active": bool(playback_active),
        "measurement_window_open": bool(measurement_window_open),
    }


def _is_qobuz_playback_active(state: dict | None) -> bool:
    return playback_state_helpers.is_external_playback_active(state)


def _has_local_footer_context(state: dict | None) -> bool:
    state = state or {}
    track = playback_state.current_track_info or state.get("current_track") or {}
    source = (track or {}).get("source")
    if not source_policy.is_mpv_source(source):
        return False
    return bool(
        state.get("current_file")
        or state.get("playing")
        or state.get("paused")
        or state.get("ended")
    )


def _derive_playback_owner_readonly(
    player_state: dict | None = None,
    spotify_state: dict | None = None,
    qobuz_state: dict | None = None,
) -> str | None:
    """Resolve a read-only owner fallback from the live active sources.

    Used only when no owner has been committed yet (e.g. right after boot,
    before the external watchers have claimed).  Never mutates the committed
    owner: a status/metadata read must not change ownership.
    """
    player_state = player_state or (runtime.player_instance.state if runtime.player_instance else {})
    spotify_state = spotify_state or playback_state.latest_spotify_state or {}
    qobuz_state = qobuz_state or playback_state.latest_qobuz_state or {}
    if _is_spotify_playback_active(spotify_state):
        return "spotify"
    if _is_qobuz_playback_active(qobuz_state):
        return "qobuz"
    if _is_local_playback_active(player_state):
        track = playback_state.current_track_info or {}
        source = (track or {}).get("source")
        return source if source_policy.is_mpv_source(source) else "local"
    return None


def _resolve_playback_owner() -> str | None:
    """Return the authoritative playback owner.

    The committed ``current_playback_owner`` is authoritative and only changes
    on a real playback intent (native play) or an external source claim.
    Pausing keeps the owner.  When nothing is committed yet, a read-only
    fallback is derived from the live active sources for display only; it is
    never persisted.
    """
    owner = playback_state.current_playback_owner
    if owner:
        return owner
    return _derive_playback_owner_readonly()


def _set_playback_owner(source: str | None) -> None:
    playback_state.current_playback_owner = source


from models import (
    PlayRequest,
)
from playback.player import get_player, MPVNotInstalledError
from playback.stream_info import StreamInfoLedger
from radio.api import _station_api_payload, router as radio_api_router
from radio.stations import get_stations
import playback.state as playback_state_helpers
from playback.state import PlaybackState
import playback.source_policy as source_policy
import audio.samplerate as samplerate
from library.core import (
    LibraryScanner,
)
from downloader import Downloader
from dsp.manager import DSPManager, ensure_kernel_supported_ir, parse_wav_frames
from dsp.crossover import crossover_response, design_crossover
from dsp.runtime import DSPRuntime, DSPRuntimeConfig, PlannedSyncTarget, _contains_link
import dsp.api as dsp_api
import dsp.orchestration as dsp_orchestration
import dsp.preset_loading as preset_loading
import playback.orchestration as playback_orchestration
from audio import pw_link
from audio.output_ports import hardware_playback_port_fallback_from_mode
from audio.output_service import MeasurementActiveError, OutputService, OutputServiceDeps
from audio.output_state import (
    FILTER_SLOPES,
    bass_crossover_for_side,
    roles_for_mode,
    shared_bass_crossover,
    routing_for_device,
    select_bank,
    set_bank_preset,
    set_bass_management,
    set_crossover,
    set_mode_extras,
    set_mode_routing,
    set_output_processing,
    switch_mode,
    validate_output_state,
)
from audio.output_state_store import OutputStateStore, StateConflictError
from audio.output_topology import MODES, SUB_ROLES, derive_topology, side_for_role
from audio.filter_banks import bank_catalog, resolve_bank, selected_bank, summarize_banks
from audio.output_state import switch_all_banks
from audio.bluetooth import BluetoothInputDependencies, BluetoothInputMonitor
from audio.drift import SamplerateDriftDependencies, SamplerateDriftObserver
from audio.external_input import ExternalInputRouting, ExternalInputRoutingDependencies
from playback.radio_reconnect import RadioReconnect, RadioReconnectDependencies
from playback.silent_active import SilentActiveDependencies, SilentActiveRecovery
from playback.spotify_watch import (
    SpotifyPlayerctlWatch,
    SpotifyWatchDependencies,
)
from playback.qobuz_watch import (
    QobuzPlayerWatch,
    QobuzWatchDependencies,
)
from playback.qbzd_volume_watch import (
    QobuzVolumeWatch,
    QobuzVolumeWatchDependencies,
)
from playback.spotifyd_volume_watch import (
    SpotifydVolumeWatch,
    SpotifydVolumeWatchDependencies,
)
from dsp.orchestration import (
    DspOrchestrationDeps,
    DspOrchestrator,
    helper_argument_sample_rate,
    with_subwoofer_derived_delays,
)
try:
    from hardware_controller import HardwareController
except ImportError:
    HardwareController = None
from measurement.store import (
    MeasurementStore,
)
from measurement.target import (
    freeze_measurement_target,
    measurement_target_from_context,
    require_commit_target,
)
from dsp.peak_monitor import DSPPeakMonitor, PeakMonitorCoordinator, PeakMonitorCoordinatorDeps
from playback.transition import (
    PlaybackTransitionCoordinator,
    PlaybackTransitionFailure,
    TransitionRequest,
)

from playback.runtime import (
    FxrouteTransitionRuntime,
    PlaybackRuntimeDependencies,
    RADIO_EXPECTED_SAMPLE_RATE_HZ,
    SOURCE_HANDOFF_SETTLE_MS,
    _playback_gate_state_path,
)


from audio.samplerate import (
    OUTPUT_MODE_STEREO,
    OUTPUT_MODE_SUBWOOFER_MODES,
    SOURCE_MODE_APP_PLAYBACK,
    SOURCE_MODE_BLUETOOTH_INPUT,
    SOURCE_MODE_EXTERNAL_INPUT,
    apply_persisted_audio_output_selection,
    get_audio_output_overview,
    get_audio_source_overview,
    get_bluetooth_audio_overview,
    get_samplerate_status,
    is_bluetooth_audio_streaming,
    normalize_sample_rate_policy,
    recover_saved_output_sink,
    set_audio_output_selection,
    set_audio_source_selection,
    set_bluetooth_receiver_enabled,
)
import streaming
from streaming.tidal import auth as tidal_auth
from streaming.tidal import playback as tidal_playback
from streaming.qobuz import connect_state
from streaming.spotify import mpris as spotify_mpris
from streaming.spotify.mpris import playerctl_available, spotify_installed
from streaming.spotify.provider import (
    SPOTIFY_PREARM_SAMPLE_RATE_HZ,
    get_status as spotify_get_status,
    play as spotify_play,
    pause as spotify_pause,
    next_track as spotify_next,
    previous as spotify_previous,
    shuffle_toggle as spotify_shuffle_toggle,
    loop_cycle as spotify_loop_cycle,
    seek_to as spotify_seek_to,
)
from audio.system_volume import SystemVolumeError, get_output_volume, set_output_volume, get_status_volume, start_volume_read_monitor, volume_percent_to_db
import audio.volume_contract as volume_contract

logger = logging.getLogger(__name__)

import install_info
import audio.power_api as power_api
import audio.canonical_volume as canonical_volume
import measurement.spl_calibration as spl_calibration
import measurement.speaker_api as speaker_api
import measurement.autosub as autosub
import measurement.session as measurement_session
from measurement.session import (
    MeasurementServices,
    MeasurementSampleRateSession,
    _measurement_helper_snapshot_summary,
    _wait_for_selected_output_effective_rate,
)
from library.api import (
    LibraryApiRuntime,
    _record_local_track_started,
    _track_cover_available,
    configure_runtime as configure_library_api_runtime,
    router as library_api_router,
)
from library.metadata import LibraryMetadataStore


@dataclass
class MusicLibraryRuntime:
    """Current music-library services and their switch serialization."""

    manager: Any = None
    scanner: Any = None
    switch_lock: Optional[asyncio.Lock] = None

    def reset(self) -> None:
        self.manager = None
        self.scanner = None
        self.switch_lock = None


@dataclass
class RuntimeResources:
    """Lifecycle-owned runtime resources created and torn down by the FastAPI lifespan.

    One authoritative location for the mutable resources that are initialized
    on startup and reset on shutdown.  Persistent configuration, manager
    singletons stay at module scope.
    """

    player_instance: Any = None
    music_library: MusicLibraryRuntime = field(default_factory=MusicLibraryRuntime)
    dsp_runtime: Any = None
    peak_monitor: Any = None
    measurement_watchdog_task: Optional[asyncio.Task] = None
    library_scan_task: Optional[asyncio.Task] = None
    spotify_state_refresh_task: Optional[asyncio.Task] = None
    spotify_state_poll_task: Optional[asyncio.Task] = None
    lifecycle_background_tasks: set[asyncio.Task] = field(default_factory=set)
    library_refresh_tasks: set[asyncio.Task] = field(default_factory=set)
    dsp_runtime_link_watch_task: Optional[asyncio.Task] = None
    dsp_preset_load_lock: Optional[asyncio.Lock] = None
    # Serializes threaded DSP mutations (convolver IR upload/create) so
    # concurrent HTTP requests cannot interleave filesystem/preset state
    # changes that used to run serially in the event loop.
    dsp_mutation_lock: Optional[asyncio.Lock] = None
    source_transition_lock: Optional[asyncio.Lock] = None
    # Serializes canonical volume writes (/api/volume, streaming volume actions) so
    # concurrent requests cannot interleave their set -> verified get -> status
    # cache publish sequences.
    canonical_volume_write_lock: Optional[asyncio.Lock] = None

    def reset(self) -> None:
        """Clear every lifecycle-owned resource at shutdown."""
        self.player_instance = None
        self.music_library.reset()
        self.dsp_runtime = None
        self.peak_monitor = None
        self.measurement_watchdog_task = None
        self.library_scan_task = None
        self.spotify_state_refresh_task = None
        self.spotify_state_poll_task = None
        self.lifecycle_background_tasks.clear()
        self.library_refresh_tasks.clear()
        self.dsp_runtime_link_watch_task = None
        self.dsp_preset_load_lock = None
        self.dsp_mutation_lock = None
        self.source_transition_lock = None
        self.canonical_volume_write_lock = None


# Global instances (initialized on startup)
settings = None
runtime = RuntimeResources()
dsp_manager = None
downloader = None
measurement_store = None
measurement_sr_session = None
hardware_controller = None

# Cooldown between player (re)start attempts from request context: a missing
# mpv binary must not turn every play press into minutes of blocking probes.
_player_restart_cooldown_until = 0.0
PLAYER_RESTART_COOLDOWN_S = 60.0


def _ensure_player_running() -> bool:
    """(Re)start the MPV player when it is missing or stopped.

    Backend-startup start failures are transient (slow first-run probes under
    load); without lazy recovery one failure 503s every /api/play until the
    service is restarted. Returns True when a running player is available.
    """
    global _player_restart_cooldown_until
    player = runtime.player_instance
    if player is not None and getattr(player, "_running", False):
        return True
    if time.monotonic() < _player_restart_cooldown_until:
        return False
    try:
        player = player if player is not None else get_player()
        player.start()
    except Exception as exc:
        logger.warning("Player (re)start failed: %s", exc)
        _player_restart_cooldown_until = time.monotonic() + PLAYER_RESTART_COOLDOWN_S
        return False
    runtime.player_instance = player
    return bool(getattr(player, "_running", False))


def _dsp_mutation_lock() -> asyncio.Lock:
    if runtime.dsp_mutation_lock is None:
        runtime.dsp_mutation_lock = asyncio.Lock()
    return runtime.dsp_mutation_lock


def _get_dsp_preset_load_lock() -> asyncio.Lock:
    if runtime.dsp_preset_load_lock is None:
        runtime.dsp_preset_load_lock = asyncio.Lock()
    return runtime.dsp_preset_load_lock


def _canonical_volume_write_lock() -> asyncio.Lock:
    if runtime.canonical_volume_write_lock is None:
        runtime.canonical_volume_write_lock = asyncio.Lock()
    return runtime.canonical_volume_write_lock


def _source_transition_lock() -> asyncio.Lock:
    if runtime.source_transition_lock is None:
        runtime.source_transition_lock = asyncio.Lock()
    return runtime.source_transition_lock


async def _drain_worker(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a sync worker off the event loop, surviving caller cancellation.

    The caller owns the surrounding critical section (lock).  A cancelled
    caller must not release that section while the worker thread is still
    running: threads cannot be cancelled, so this helper waits until the
    worker actually finished, then re-raises CancelledError.  Worker
    exceptions are re-raised in the normal path and logged on the
    cancellation path (the cancellation takes precedence).
    """
    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        try:
            worker.result()
        except BaseException:
            logger.exception("Worker failed while its caller was cancelled")
        raise asyncio.CancelledError
    return worker.result()


async def _run_locked_worker(lock: asyncio.Lock, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a sync worker inside a critical section that survives cancellation.

    The lock is only released after the worker actually finished, even when
    the caller is cancelled.
    """
    async with lock:
        return await _drain_worker(func, *args, **kwargs)
playback_transition_coordinator: PlaybackTransitionCoordinator | None = None
# Measurement-window heartbeat timestamp: set by
# /api/power/measurement-heartbeat, read only via _is_measurement_window_open().
# It is measurement-window/power state, not silent-active recovery, so it
# stays a plain module scalar rather than joining SilentActiveRecoveryState.
last_measurement_window_seen_at = 0.0
# Wait primitive for ended callbacks: set once no playback transition
# is in flight.  asyncio.Event binds to the first used event loop; tests
# use a fresh loop per test, so the signal is kept per loop (production:
# exactly one loop).  No second transition lock.
_playback_settled_events: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Event]" = weakref.WeakKeyDictionary()


def _playback_settled_event() -> asyncio.Event:
    """Return the settle signal for the running event loop (create if new)."""
    loop = asyncio.get_running_loop()
    event = _playback_settled_events.get(loop)
    if event is None:
        event = asyncio.Event()
        event.set()
        _playback_settled_events[loop] = event
    return event


# Single authoritative owner of the mutable playback/transition state
# (current/last track, Spotify UI state, footer owner, intent generation,
# transition epoch/pending attempts and the published commit tokens).
# See playback_state.PlaybackState for the field relationships.
playback_state = PlaybackState()

# Last known MPV stream facts per track, so rate/status refreshes never
# degrade a complete radio/local/TIDAL track's quality data during a
# transient telemetry gap (see playback.stream_info.StreamInfoLedger).
stream_info_ledger = StreamInfoLedger()


radio_reconnect = RadioReconnect(RadioReconnectDependencies(
    get_player_instance=lambda: runtime.player_instance,
    get_playback_state=lambda: playback_state,
    request_coordinated_recovery=lambda *args, **kwargs: playback_orchestration.configured().request_coordinated_recovery(*args, **kwargs),
))

silent_active_recovery = SilentActiveRecovery(SilentActiveDependencies(
    get_peak_monitor=lambda: runtime.peak_monitor,
    get_player_instance=lambda: runtime.player_instance,
    get_current_track_info=lambda: playback_state.current_track_info,
    get_current_playback_owner=lambda: _resolve_playback_owner(),
    get_spotify_ui_state=lambda *args, **kwargs: get_spotify_ui_state(*args, **kwargs),
    list_mpv_sink_inputs=lambda: media_readiness.list_mpv_sink_inputs(),
    list_spotify_sink_inputs=lambda: media_readiness.list_spotify_sink_inputs(),
    list_all_sink_inputs=lambda: media_readiness.list_sink_inputs(),
    get_output_volume_safe=lambda default=100: get_output_volume_safe(default),
    run_debug_command=lambda args, timeout=2.0: _run_debug_command(args, timeout),
    is_measurement_window_open=lambda: _is_measurement_window_open(),
    dsp_preset_load_locked=lambda: runtime.dsp_preset_load_lock is not None and runtime.dsp_preset_load_lock.locked(),
    current_track_matches=lambda track: _current_track_matches(track),
))

peak_monitor_coordinator = PeakMonitorCoordinator(PeakMonitorCoordinatorDeps(
    get_peak_monitor=lambda: runtime.peak_monitor,
    get_player_state=lambda: runtime.player_instance.state if runtime.player_instance else {},
    get_current_track_info=lambda: playback_state.current_track_info,
    broadcast=lambda message: manager.broadcast(message),
    get_spotify_ui_state=lambda *args, **kwargs: get_spotify_ui_state(*args, **kwargs),
    get_qobuz_ui_state=lambda *args, **kwargs: get_qobuz_ui_state(*args, **kwargs),
    get_audio_source_overview=lambda: get_audio_source_overview(),
    capture_transition_epoch=lambda *a, **k: _capture_playback_transition_epoch(*a, **k),
    transition_context_is_current=lambda *a, **k: _playback_transition_context_is_current(*a, **k),
    transition_is_active=lambda: _playback_transition_is_active(),
    sleep=lambda delay: asyncio.sleep(delay),
))

external_input = ExternalInputRouting(ExternalInputRoutingDependencies(
    get_audio_source_overview=lambda: get_audio_source_overview(),
))

bluetooth_input = BluetoothInputMonitor(BluetoothInputDependencies(
    sync_peak_monitor_for_source_mode_state=lambda overview=None: peak_monitor_coordinator.sync_source_mode_state(overview),
    get_persisted_source_mode=lambda: (
        samplerate._load_audio_source_selection().get("mode") or SOURCE_MODE_APP_PLAYBACK
    ),
))

samplerate_drift = SamplerateDriftObserver(SamplerateDriftDependencies(
    get_current_track_info=lambda: playback_state.current_track_info,
    get_player_instance=lambda: runtime.player_instance,
    coordinator_source_rate=lambda *args, **kwargs: playback_orchestration.configured().coordinator_source_rate(*args, **kwargs),
    get_player_audio_samplerate=lambda: media_readiness.get_player_audio_samplerate(runtime.player_instance),
    get_samplerate_status=lambda: get_samplerate_status(),
    playback_transition_is_active=lambda: _playback_transition_is_active(),
    is_measurement_window_open=lambda: _is_measurement_window_open(),
    measurement_session_active=lambda: measurement_sr_session is not None and measurement_sr_session.active,
    measurement_audio_graph_owned=lambda: playback_orchestration.configured().measurement_audio_graph_owned(),
    request_coordinated_recovery=lambda *args, **kwargs: playback_orchestration.configured().request_coordinated_recovery(*args, **kwargs),
))

spotify_playerctl_watch = SpotifyPlayerctlWatch(SpotifyWatchDependencies(
    get_playback_state=lambda: playback_state,
    get_spotify_ui_state=lambda *args, **kwargs: get_spotify_ui_state(*args, **kwargs),
    list_spotify_sink_inputs=lambda: media_readiness.list_spotify_sink_inputs(),
    spotify_sink_input_observation=lambda *args, **kwargs: media_readiness.spotify_sink_input_observation(*args, **kwargs),
    request_coordinated_recovery=lambda *args, **kwargs: playback_orchestration.configured().request_coordinated_recovery(*args, **kwargs),
    schedule_spotify_state_refresh=lambda reason: _schedule_spotify_state_refresh(reason),
    claim_spotify_playback=lambda *args, **kwargs: _claim_spotify_playback(*args, **kwargs),
))
qobuz_player_watch = QobuzPlayerWatch(QobuzWatchDependencies(
    get_playback_state=lambda: playback_state,
    broadcast_qobuz_state=lambda *args, **kwargs: broadcast_qobuz_state(*args, **kwargs),
    is_qobuz_playback_active=lambda *args, **kwargs: _is_qobuz_playback_active(*args, **kwargs),
    claim_qobuz_playback=lambda *args, **kwargs: _claim_qobuz_playback(*args, **kwargs),
))
qobuz_volume_watch = QobuzVolumeWatch(QobuzVolumeWatchDependencies(
    is_active=lambda: _resolve_playback_owner() == "qobuz" and connect_state.is_device_active() is not False,
    apply_volume_value=lambda value: _apply_remote_volume_value(
        value,
        owner="qobuz",
        source_active=lambda: connect_state.is_device_active() is not False,
    ),
    current_master=lambda: get_output_volume_safe(),
    on_device_active=lambda value: connect_state.set_device_active(value),
))
spotifyd_volume_watch = SpotifydVolumeWatch(SpotifydVolumeWatchDependencies(
    is_active=lambda: _resolve_playback_owner() == "spotify",
    apply_volume_value=lambda value: _apply_remote_volume_value(value, owner="spotify"),
    current_master=lambda: get_output_volume_safe(),
))
radio_metadata_service = RadioMetadataService()
# queue_advancing is a reentrancy/dispatch guard for
# on_player_state_change and deliberately not queue state: the queue
# state (list, original order, index, mode, loop, shuffle, single-track-
# loop) lives exclusively in playback.queue.PlaybackQueue.
queue_advancing = False

def _sync_playback_track_favorite(track_id: str, favorite: bool) -> None:
    """Mirror a persisted track favorite into live playback/queue track dicts.

    The library mutation updates the scanner's in-memory Track objects, but
    the currently playing track and the queue hold independent dict copies;
    without this the next status/WS poll would revert the footer heart.
    """
    if playback_state.current_track_info and playback_state.current_track_info.get("id") == track_id:
        playback_state.current_track_info["favorite"] = favorite
    if playback_state.last_track_info and playback_state.last_track_info.get("id") == track_id:
        playback_state.last_track_info["favorite"] = favorite
    queue = playback_queue.queue
    if queue is not None:
        for track in queue.tracks:
            if track.get("id") == track_id:
                track["favorite"] = favorite


configure_library_api_runtime(LibraryApiRuntime(
    get_scanner=lambda: runtime.music_library.scanner,
    get_settings=lambda: settings,
    run_blocking=_drain_worker,
    sync_track_favorite=_sync_playback_track_favorite,
))
spl_calibration.configure_runtime(spl_calibration.SplCalibrationDependencies(
    get_measurement_store=lambda: measurement_store,
    get_measurement_session=lambda: measurement_sr_session,
    get_dsp_manager=lambda: dsp_manager,
    require_dsp_manager=lambda: _require_dsp_manager(),
    get_output_volume=lambda: get_output_volume(),
    set_output_volume=lambda value: set_output_volume(value),
    read_measurement_settings=lambda: measurement_session._read_measurement_setup_settings(),
    measurement_entry_preflight=lambda rate: measurement_session._measurement_entry_preflight(rate),
    run_dsp_mutation=lambda func: _run_locked_worker(
        _dsp_mutation_lock(), func
    ),
))
def _make_measurement_services() -> MeasurementServices:
    """Bind the /api/measurements* module to the application services.

    All entries resolve the current runtime state at call time, so tests that
    patch main attributes observe the patched services.
    """
    return MeasurementServices(
        get_store=lambda: measurement_store,
        get_session=lambda: measurement_sr_session,
        auto_sub_active=lambda: autosub.is_optimization_active() or (
            _speaker_align_service_instance is not None and _speaker_align_service_instance.active),
        get_dsp_runtime=lambda: runtime.dsp_runtime,
        get_player=lambda: runtime.player_instance,
        get_samplerate_status=lambda *a, **k: get_samplerate_status(*a, **k),
        get_audio_output_overview=lambda *a, **k: get_audio_output_overview(*a, **k),
        get_current_track_info=lambda: playback_state.current_track_info,
        get_playback_transition_coordinator=lambda: playback_transition_coordinator,
        get_dsp_orchestrator=lambda: dsp_orchestrator,
        get_playback_intent_generation=lambda: playback_state.playback_intent_generation,
        run_coordinated_transition=lambda *a, **k: _run_coordinated_transition(*a, **k),
        coordinator_current_playback_context=lambda *a, **k: _coordinator_current_playback_context(*a, **k),
        begin_playback_transition_attempt=lambda *a, **k: _begin_playback_transition_attempt(*a, **k),
        end_playback_transition_attempt=lambda *a, **k: _end_playback_transition_attempt(*a, **k),
        get_current_pipewire_force_rate=lambda *a, **k: samplerate.get_current_pipewire_force_rate(*a, **k),
        set_pipewire_force_rate=lambda *a, **k: samplerate.set_pipewire_force_rate(*a, **k),
        ensure_playback_samplerate_force=lambda *a, measurement_blocks_rate=_measurement_blocks_playback_rate, **k: samplerate.ensure_playback_samplerate_force(
            *a, measurement_blocks_rate=measurement_blocks_rate, **k
        ),
        wait_for_samplerate_alignment=lambda *a, **k: samplerate.wait_for_samplerate_alignment(*a, **k),
        reconcile_transition_sink_rate=lambda *a, measurement_blocks_rate=_measurement_blocks_playback_rate, **k: samplerate.reconcile_transition_sink_rate(
            *a, measurement_blocks_rate=measurement_blocks_rate, **k
        ),
        playback_graph_diagnosis=lambda *a, **k: playback_orchestration.configured().playback_graph_diagnosis(*a, **k),
        log_playback_graph_diagnosis=lambda *a, **k: playback_orchestration.configured().log_playback_graph_diagnosis(*a, **k),
        measurement_restore_intent_matches_live_state=lambda *a, **k: _measurement_restore_intent_matches_live_state(*a, **k),
        spotify_snapshot_identity_values=lambda *a, **k: _spotify_snapshot_identity_values(*a, **k),
        spotify_target_track_from_state=lambda *a, **k: _spotify_target_track_from_state(*a, **k),
        get_player_audio_samplerate=lambda *a, **k: media_readiness.get_player_audio_samplerate(runtime.player_instance),
        pulse_suspend_sink_for_samplerate=lambda *a, **k: samplerate.pulse_suspend_sink_for_samplerate(*a, **k),
        audio_output_overview_with_effective_rate=lambda *a, **k: samplerate.audio_output_overview_with_effective_rate(*a, **k),
        spotify_prearm_sample_rate_hz=SPOTIFY_PREARM_SAMPLE_RATE_HZ,
        pipewire_handoff_poll_interval_ms=media_readiness.PIPEWIRE_HANDOFF_POLL_INTERVAL_MS,
        build_autosub_release_adapter=lambda *, output_key, channels: _create_autosub_release_adapter(
            service=get_output_service(), output_key=output_key, channels=channels),
        stage_bank_v2_context=lambda *, measurement_bank, measurement_rate_hz: _stage_bank_v2_context(
            measurement_bank=measurement_bank, measurement_rate_hz=measurement_rate_hz),
    )


measurement_session.configure_services(_make_measurement_services())
speaker_api.configure_speaker_align(lambda: get_speaker_align_service())
autosub.configure_dependencies(autosub.AutoSubDependencies(
    get_dsp_runtime=lambda: runtime.dsp_runtime,
    get_measurement_store=lambda: measurement_store,
    get_measurement_session=lambda: measurement_sr_session,
    get_dsp_manager=lambda: dsp_manager,
    get_output_service=lambda: get_output_service(),
    create_candidate_session=lambda **kwargs: _create_auto_sub_candidate_session(**kwargs),
))

def _set_runtime_current_track_info(value: dict | None) -> None:
    playback_state.current_track_info = value


def _set_runtime_playback_owner(value: str | None) -> None:
    playback_state.current_playback_owner = value


def _set_runtime_track_context(current: dict, last: dict) -> None:
    playback_state.set_track_context(current, last)


def make_playback_runtime_deps() -> PlaybackRuntimeDependencies:
    """Late-bound wiring for ``FxrouteTransitionRuntime`` (playback/runtime/).

    Every accessor resolves the current runtime state at call time, so
    production wiring and test mocks observe the same attributes (same
    contract as the ``configure_*`` pattern used by the extracted routers).
    """
    return PlaybackRuntimeDependencies(
        player=lambda: runtime.player_instance,
        dsp_manager=lambda: dsp_manager,
        dsp_runtime=lambda: runtime.dsp_runtime,
        get_current_track_info=lambda: playback_state.current_track_info,
        set_current_track_info=_set_runtime_current_track_info,
        get_playback_intent_generation=lambda: playback_state.playback_intent_generation,
        get_transition_epoch=lambda: playback_state.playback_transition_epoch,
        set_playback_owner=_set_runtime_playback_owner,
        queue=lambda: playback_queue.queue,
        player_is_running=lambda *a, **k: _player_is_running(*a, **k),
        load_player_paused=lambda *a, **k: _load_player_paused(*a, **k),
        wait_for_player_current_file=lambda *a, **k: media_readiness.wait_for_player_current_file(*a, get_player=lambda: runtime.player_instance, **k),
        wait_for_player_audio_samplerate=lambda *a, **k: media_readiness.wait_for_player_audio_samplerate(*a, get_player=lambda: runtime.player_instance, drain_worker=_drain_worker, **k),
        get_player_audio_samplerate=lambda *a, **k: media_readiness.get_player_audio_samplerate(runtime.player_instance),
        wait_for_radio_live_rate_after_load=lambda *a, **k: media_readiness.wait_for_radio_live_rate_after_load(*a, get_epoch=lambda: playback_state.playback_transition_epoch, drain_worker=_drain_worker, get_player=lambda: runtime.player_instance, **k),
        wait_for_pipewire_mpv_release=lambda *a, **k: media_readiness.wait_for_pipewire_mpv_release(*a, **k),
        wait_for_pipewire_spotify_release=lambda *a, **k: media_readiness.wait_for_pipewire_spotify_release(*a, **k),
        wait_for_spotify_sink_input_samplerate=lambda *a, **k: media_readiness.wait_for_spotify_sink_input_samplerate(*a, **k),
        get_samplerate_status=lambda *a, **k: get_samplerate_status(*a, **k),
        get_audio_output_overview=lambda *a, **k: get_audio_output_overview(*a, **k),
        ensure_playback_samplerate_force=lambda *a, measurement_blocks_rate=_measurement_blocks_playback_rate, **k: samplerate.ensure_playback_samplerate_force(
            *a, measurement_blocks_rate=measurement_blocks_rate, **k
        ),
        trigger_idle_sink_renegotiation=lambda *a, **k: samplerate.trigger_idle_sink_renegotiation(*a, **k),
        recover_stale_samplerate_helper=lambda *a, **k: dsp_orchestrator.recover_stale_helper_samplerate(*a, **k),
        reconcile_transition_sink_rate=lambda *a, measurement_blocks_rate=_measurement_blocks_playback_rate, **k: samplerate.reconcile_transition_sink_rate(
            *a, measurement_blocks_rate=measurement_blocks_rate, **k
        ),
        coordinator_source_rate=lambda *a, **k: playback_orchestration.configured().coordinator_source_rate(*a, **k),
        coordinator_target_rate=lambda *a, **k: _coordinator_target_rate(*a, **k),
        spotify_pause=lambda *a, **k: spotify_pause(*a, **k),
        get_spotify_ui_state=lambda *a, **k: get_spotify_ui_state(*a, **k),
        is_spotify_playback_active=lambda *a, **k: _is_spotify_playback_active(*a, **k),
        has_local_footer_context=lambda *a, **k: _has_local_footer_context(*a, **k),
        pause_spotify_for_local_playback_broadcast=lambda *a, **k: pause_spotify_for_local_playback_broadcast(*a, **k),
        pause_local_playback_for_spotify_broadcast=lambda *a, **k: pause_local_playback_for_spotify_broadcast(*a, **k),
        get_qobuz_ui_state=lambda *a, **k: get_qobuz_ui_state(*a, **k),
        is_qobuz_playback_active=lambda *a, **k: _is_qobuz_playback_active(*a, **k),
        qobuz_play=lambda *a, **k: qobuz_play(*a, **k),
        qobuz_pause=lambda *a, **k: qobuz_pause(*a, **k),
        wait_for_pipewire_qobuz_release=lambda *a, **k: media_readiness.wait_for_pipewire_qobuz_release(*a, **k),
        wait_for_qobuz_sink_input_samplerate=lambda *a, **k: media_readiness.wait_for_qobuz_sink_input_samplerate(*a, **k),
        mark_player_state_authoritative=lambda *a, **k: _mark_player_state_authoritative(*a, **k),
        spotify_snapshot_identity_values=lambda *a, **k: _spotify_snapshot_identity_values(*a, **k),
        measurement_restore_intent_matches_live_state=lambda *a, **k: _measurement_restore_intent_matches_live_state(*a, **k),
        measurement_audio_graph_owned=lambda: playback_orchestration.configured().measurement_audio_graph_owned(),
        dsp_mutation_lock=lambda: _dsp_mutation_lock(),
        drain_worker=lambda *a, **k: _drain_worker(*a, **k),
        load_dsp_preset=lambda *a, **k: _load_dsp_preset(*a, **k),
        sync_dsp_runtime=lambda *a, **k: dsp_orchestrator.sync_runtime(*a, **k),
        helper_argument_sample_rate=dsp_orchestration.helper_argument_sample_rate,
        playback_graph_diagnosis=lambda *a, **k: playback_orchestration.configured().playback_graph_diagnosis(*a, **k),
        measurement_session_link_loss_is_repairable=lambda *a, **k: playback_orchestration.configured().measurement_session_link_loss_is_repairable(*a, **k),
        coordinator_reconcile_subwoofer_links_only=lambda *a, **k: playback_orchestration.configured().reconcile_subwoofer_links_only(*a, **k),
        repair_stereo_output_links_once=lambda *a, **k: playback_orchestration.configured().repair_stereo_output_links_once(*a, **k),
        coordinator_establish_effects_and_helper=lambda *a, **k: playback_orchestration.configured().establish_effects_and_helper(*a, **k),
        ensure_mpv_to_dsp_links=lambda *a, **k: playback_orchestration.configured().ensure_mpv_to_dsp_links(*a, **k),
        playback_graph_links_complete=lambda *a, **k: playback_orchestration.configured().playback_graph_links_complete(*a, **k),
        log_playback_graph_diagnosis=lambda *a, **k: playback_orchestration.configured().log_playback_graph_diagnosis(*a, **k),
        coordinator_reconcile_post_start_graph=lambda *a, **k: playback_orchestration.configured().reconcile_post_start_graph(*a, **k),
        audio_configuration_lock=lambda: measurement_sr_session.lock,
        get_output_service=lambda: get_output_service(),
    )


playback_queue.configure_playback_queue(playback_queue.PlaybackQueueDependencies(
    player=lambda: runtime.player_instance,
    run_transition=lambda *a, **k: _run_coordinated_transition(*a, **k),
    commit_coordinated_track=lambda *a, **k: _commit_coordinated_track(*a, **k),
    get_current_track_info=lambda: playback_state.current_track_info,
    set_track_context=_set_runtime_track_context,
    transition_is_active=lambda: _playback_transition_is_active(),
    player_is_running=lambda *a, **k: _player_is_running(*a, **k),
    wait_for_player_current_file=lambda *a, **k: media_readiness.wait_for_player_current_file(*a, get_player=lambda: runtime.player_instance, **k),
    coordinator_target_rate=lambda *a, **k: _coordinator_target_rate(*a, **k),
    coordinator_rate_change=lambda *a, **k: _coordinator_rate_change(*a, **k),
    sample_rate_policy_is_auto=lambda: _sample_rate_policy_is_auto(),
    transition_error_http=lambda exc: _transition_error_http(exc),
    get_tracks=lambda: runtime.music_library.scanner.get_tracks(),
    build_playback_payload=lambda *a, **k: build_playback_payload(*a, **k),
    resolve_stream_url=lambda track: _resolve_tidal_stream_url(track),
    capture_source_intent=lambda: playback_state.capture_source_intent(),
    ensure_source_intent_current=lambda captured: _ensure_source_intent_current(captured),
))

# Bind the native TIDAL provider's now-playing reflection to the FXRoute
# playback owner (MPV -> fxroute_dsp_sink), so /api/streaming/tidal/status
# mirrors the committed tidal source without owning its own transport.
streaming.get_provider("tidal").configure(lambda: build_playback_payload())


def _begin_playback_transition_attempt() -> int:
    epoch = playback_state.begin_transition_attempt()
    _playback_settled_event().clear()
    return epoch


def _end_playback_transition_attempt() -> None:
    if playback_state.end_transition_attempt():
        _playback_settled_event().set()


def _capture_playback_transition_epoch() -> int | None:
    """Capture a playback-context token; None while an attempt is in flight.

    A token captured while any attempt is active must stay invalid forever,
    matching the legacy odd/even generation contract: only idle captures may
    ever become a committed context.
    """
    return playback_state.capture_transition_epoch()


def _current_playback_commit_id() -> str | None:
    """Return the published playback-context token.

    Unlike the Coordinator's ``last_successful_commit_id`` (which advances on
    every committed Coordinator operation, including output-mode-switch,
    measurement-entry/restore, sample-rate-policy and recovery), this token
    is only published at the application commit boundary together with the
    authoritative playback globals.  It stays unchanged while an attempt is
    running, after it failed, and after non-source-changing commits.
    """
    return playback_state.current_playback_commit_id()


@asynccontextmanager
async def _manual_transport_guard(expected_epoch: int | None = None):
    """Serialize one manual transport mutation against the Coordinator.

    The epoch is captured before any await (or supplied by callers that
    awaited before entering), so a transition that starts, or starts and
    completes, while the caller was waiting invalidates the action instead of
    letting it apply to a newer committed context.
    """
    coordinator = playback_transition_coordinator
    lock = getattr(coordinator, "lock", None)
    if lock is None:
        if (
            _playback_transition_is_active()
            or (
                expected_epoch is not None
                and playback_state.playback_transition_epoch != expected_epoch
            )
        ):
            raise HTTPException(status_code=409, detail="A playback transition is in progress")
        yield
        return

    captured_epoch = (
        expected_epoch
        if expected_epoch is not None
        else playback_state.playback_transition_epoch
    )
    async with lock:
        if playback_state.playback_transition_epoch != captured_epoch:
            raise HTTPException(status_code=409, detail="A playback transition completed first")
        yield


def _publish_playback_context_commit(commit_token: str | None) -> None:
    """Publish the playback-context token exactly once at the app boundary.

    Synchronous: callers invoke this immediately after all globals of the new
    authoritative playback context were committed and before any further
    await, so the Ended-Waiter can never observe a window with a new token
    but old playback globals (or vice versa).
    """
    playback_state.publish_playback_context_commit(commit_token)


async def _publish_committed_playback_owner(owner: str, transition_id: str | None) -> None:
    """Commit the authoritative owner and publish it on the playback channel.

    Runs after a successful source handoff commit.  The playback broadcast is
    the single authoritative owner channel for the browser: provider status
    payloads carry ``playback_owner`` for display only and must never be the
    freshest copy the frontend resolves against.  Publishing here keeps the
    owner and the committed context token on the same broadcast path.

    A committed owner change is a playback intent: advancing the intent
    generation here also invalidates queued external claims of the *other*
    provider (a stale Spotify claim must not resume over a newer Qobuz start
    and vice versa).
    """
    playback_state.mark_playback_intent_changed()
    playback_state.current_playback_owner = owner
    _publish_playback_context_commit(transition_id)
    player_state = runtime.player_instance.state if runtime.player_instance else None
    await manager.broadcast({
        "type": "playback",
        "data": build_playback_payload(player_state),
    })


async def _wait_playback_transition_settled() -> None:
    """Wait until no playback-transition attempt is pending (terminal state).

    Waits on the settled event which is cleared on every attempt start and
    set when the last attempt finished.  Multiple queued attempts (nested or
    serialized behind the Coordinator lock) are all covered, because the
    counter only reaches zero after the final attempt drained.  Never holds
    the Coordinator lock and never blocks the event loop.
    """
    while playback_state.playback_transition_pending_attempts > 0:
        event = _playback_settled_event()
        if not event.is_set():
            await event.wait()
        else:
            # Defensive yield: a set event with pending attempts violates the
            # begin/end invariant; re-check after yielding instead of spinning.
            await asyncio.sleep(0)


def _player_is_running(player=None) -> bool:
    """Return player availability without requiring a concrete MPV class.

    The production wrapper exposes ``_running``.  Keeping the adapter tolerant
    of small player doubles is useful for the coordinator's failure-path tests
    and does not weaken the production check: an explicit ``False`` still
    means unavailable.
    """
    player = player if player is not None else runtime.player_instance
    return bool(player is not None and getattr(player, "_running", True))


def _load_player_paused(path: str) -> None:
    """Load a target through the explicit paused-load contract."""
    if not _player_is_running():
        raise RuntimeError("MPV player is not available")
    runtime.player_instance.set_pause(True)
    try:
        runtime.player_instance.loadfile(path, mode="replace", start_paused=True)
    except TypeError as exc:
        # Compatibility for a minimal adapter that predates the explicit
        # keyword.  The real MPVWrapper implements start_paused; the fallback
        # still keeps the source paused before and after load.
        if "start_paused" not in str(exc) and "keyword" not in str(exc):
            raise
        runtime.player_instance.loadfile(path, mode="replace")
    runtime.player_instance.set_pause(True)


def _normalize_spotify_identity(value: Any) -> str | None:
    """Return a stable comparison key for a Spotify track identity."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("spotify:track:"):
        track_id = raw.split(":", 2)[2].split("?", 1)[0].strip("/")
        return f"spotify:track:{track_id}" if track_id else None

    parsed = urlparse(raw)
    if parsed.scheme.lower() == "spotify":
        parts = [part for part in (parsed.netloc, *parsed.path.strip("/").split("/")) if part]
        if len(parts) >= 2 and parts[0].lower() == "track":
            return f"spotify:track:{parts[1]}"
    if parsed.netloc:
        parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
        for index, part in enumerate(parts[:-1]):
            if part.lower() == "track":
                return f"spotify:track:{parts[index + 1]}"
    return raw


def _spotify_identity_values(value: Mapping[str, Any] | None) -> set[str]:
    """Collect stable Spotify identity candidates from a live/snapshot mapping."""
    if not isinstance(value, Mapping):
        return set()
    identities: set[str] = set()
    for key in (
        "trackId",
        "trackid",
        "spotify_track_id",
        "spotify_identity",
        "id",
        "url",
        "uri",
        "path",
        "spotify_url",
        "target_url",
    ):
        normalized = _normalize_spotify_identity(value.get(key))
        if not normalized:
            continue
        identities.add(normalized)
        if normalized.startswith("spotify:track:"):
            identities.add(normalized.rsplit(":", 1)[-1])
    return identities


def _spotify_snapshot_identity_values(snapshot: Mapping[str, Any] | None) -> set[str]:
    """Collect Spotify identity from both snapshot fields and its track record."""
    if not isinstance(snapshot, Mapping):
        return set()
    identities = _spotify_identity_values(snapshot)
    for key in ("track_info", "target_track", "spotify"):
        nested = snapshot.get(key)
        if isinstance(nested, Mapping):
            identities.update(_spotify_identity_values(nested))
    return identities


def _spotify_target_track_from_state(state: Mapping[str, Any]) -> dict[str, Any]:
    track_id = state.get("trackId")
    return {
        "source": "spotify",
        "id": track_id,
        "url": track_id or state.get("url"),
        "title": state.get("title"),
        "artist": state.get("artist"),
        "sample_rate_hz": SPOTIFY_PREARM_SAMPLE_RATE_HZ,
    }


def _transition_error_http(exc: PlaybackTransitionFailure) -> HTTPException:
    return HTTPException(status_code=500, detail=exc.as_status())


def _mark_playback_intent_changed() -> None:
    """Advance the measurement-restore intent token after a user action."""
    playback_state.mark_playback_intent_changed()


def _schedule_tidal_prefetch() -> None:
    """Fire-and-forget a background DASH download for the next TIDAL queue track.

    Called after every coordinator commit that establishes a new current track.
    Runs the existing :func:`~streaming.tidal.playback.resolve_stream_for_id`
    in a thread-pool worker so the DASH ``.mp4`` is already cached when a
    subsequent Next or automatic queue advance fires the normal play path.
    Errors are intentionally not surfaced — prefetch is a pure optimisation.
    """
    import threading

    tracks = playback_queue.queue.tracks
    index = playback_queue.queue.index
    if index < 0 or index + 1 >= len(tracks):
        return
    next_track = dict(tracks[index + 1])
    if str(next_track.get("source") or "") != "tidal":
        return
    next_id = str(next_track.get("id") or "")
    if not next_id:
        return
    from streaming.tidal.playback import prefetch_stream

    threading.Thread(target=prefetch_stream, args=(next_id,), daemon=True).start()


def _capture_source_intent() -> tuple[str, int]:
    """Capture the source commit boundary for one playback intent.

    Call synchronously at intent start (before the first await) so a source
    switch that commits while the transition runs invalidates the later app
    publish instead of committing stale queue/track/owner state over it.
    """
    return playback_state.capture_source_intent()


def _ensure_source_intent_current(captured: tuple[str, int] | None) -> None:
    """Reject an app publish whose source boundary moved during the intent."""
    if not playback_state.source_intent_is_current(captured):
        raise HTTPException(status_code=409, detail="Audio source changed during playback transition")


def _source_changed_skip(result: Any) -> bool:
    """Return whether a transition result is a source-change discard."""
    state = getattr(result, "state", None) or {}
    if not isinstance(state, Mapping):
        return False
    return bool(state.get("skipped")) and str(state.get("reason") or "") == "source-changed"


def _raise_for_uncommitted_transition(result: Any, *, what: str) -> None:
    """Map an uncommitted coordinator outcome to 409 (source) or 500."""
    if _source_changed_skip(result):
        raise HTTPException(status_code=409, detail=f"Audio source changed during {what}")
    raise HTTPException(status_code=500, detail="Playback transition was not committed")


def _commit_coordinated_track(
    track_info: Mapping[str, Any],
    *,
    source: str,
    commit_token: str | None = None,
) -> None:
    track = dict(track_info)
    _mark_playback_intent_changed()
    playback_state.current_track_info = track
    playback_state.last_track_info = track
    playback_state.current_playback_owner = source if source_policy.is_known_source(source) else None
    if source == "radio":
        playback_state.last_radio_track_info = dict(track)
    if source == "local":
        _record_local_track_started(track)
    _mark_player_state_authoritative(runtime.player_instance.state if runtime.player_instance else {})
    # Publish the new playback context token only here, after all globals
    # belonging to the context were committed: the ended waiter can never
    # run between the coordinator commit and this boundary (the boundary
    # is published synchronously in the same caller step).
    _publish_playback_context_commit(commit_token)
    # Optimistically warm the DASH cache for the next TIDAL queue track so a
    # subsequent Next or automatic queue advance hits a ready cache file.
    if source == "tidal":
        _schedule_tidal_prefetch()


# WebSocket connection manager
manager = ConnectionManager()


def _current_track_matches(expected_track: dict | None) -> bool:
    if not expected_track:
        return False
    live_track = playback_state.current_track_info or {}
    if not (
        live_track.get("source") == expected_track.get("source")
        and live_track.get("url") == expected_track.get("url")
        and live_track.get("id") == expected_track.get("id")
    ):
        return False
    expected_url = expected_track.get("url")
    current_file = (runtime.player_instance.state if runtime.player_instance else {}).get("current_file")
    if expected_url and current_file and current_file != expected_url:
        return False
    return True


def _playback_state_matches_track(state: dict | None, track: dict | None) -> bool:
    return playback_state_helpers.playback_state_matches_track(state, track)


def _create_lifecycle_background_task(coro, *, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    runtime.lifecycle_background_tasks.add(task)
    task.add_done_callback(runtime.lifecycle_background_tasks.discard)
    return task


async def _json_object(request: Request, *, detail: str = "Invalid JSON body") -> dict:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=detail) from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail=detail)
    return body


def _create_library_refresh_task(scanner: LibraryScanner, *, name: str) -> asyncio.Task:
    task = asyncio.create_task(asyncio.to_thread(scanner.refresh, True), name=name)
    runtime.library_refresh_tasks.add(task)
    task.add_done_callback(runtime.library_refresh_tasks.discard)
    return task


def _music_library_lock() -> asyncio.Lock:
    if runtime.music_library.switch_lock is None:
        runtime.music_library.switch_lock = asyncio.Lock()
    return runtime.music_library.switch_lock


def _library_scanner_for(root: Path, library_id: str = "local") -> LibraryScanner:
    if library_id == "local":
        return LibraryScanner(root)
    cache_key = hashlib.sha256(library_id.encode()).hexdigest()[:12]
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "fxroute"
    store = LibraryMetadataStore(
        config_dir / f"library-metadata-{cache_key}.sqlite",
        config_dir / f"library-metadata-covers-{cache_key}",
    )
    return LibraryScanner(root, metadata_store=store)


async def _spotify_intent_matches_live_state(
    expected_identities: set[str],
    intent_generation: Any,
) -> bool:
    """Return whether the live Spotify state still matches a captured intent."""
    try:
        spotify_state = await get_spotify_ui_state()
    except Exception:
        return False
    live_identities = _spotify_identity_values(spotify_state)
    status = str(spotify_state.get("status") or "").strip().lower()
    if status not in {"playing", "paused"}:
        return False
    if not expected_identities or not live_identities.intersection(expected_identities):
        return False
    return _measurement_restore_intent_generation_matches(intent_generation)


def _local_intent_matches_live_state(
    *,
    expected_source: str,
    expected_id: Any,
    expected_url: str | None,
    expected_file: str | None,
    intent_generation: Any,
) -> bool:
    """Return whether the live MPV/local context still matches a captured intent."""
    live_track = playback_state.current_track_info or {}
    if str(live_track.get("source") or "") != expected_source:
        return False
    if expected_id is not None and live_track.get("id") != expected_id:
        return False
    if expected_url and live_track.get("url") != expected_url:
        return False

    state = dict(runtime.player_instance.state if runtime.player_instance else {})
    current_file = state.get("current_file")
    if not current_file or state.get("ended"):
        return False
    if expected_file and current_file != expected_file:
        return False
    return _measurement_restore_intent_generation_matches(intent_generation)


def _measurement_restore_intent_generation_matches(intent_generation: Any) -> bool:
    return not (
        isinstance(intent_generation, int)
        and intent_generation != playback_state.playback_intent_generation
    )


async def _measurement_restore_intent_matches_live_state(
    *,
    expected_source: str,
    expected_id: Any,
    expected_url: str | None,
    expected_file: str | None,
    expected_spotify_identities: set[str],
    intent_generation: Any,
) -> bool:
    if expected_source == "spotify":
        return await _spotify_intent_matches_live_state(
            expected_spotify_identities,
            intent_generation,
        )
    return _local_intent_matches_live_state(
        expected_source=expected_source,
        expected_id=expected_id,
        expected_url=expected_url,
        expected_file=expected_file,
        intent_generation=intent_generation,
    )


def _run_debug_command(args: list[str], timeout: float = 2.0) -> dict:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
        }
    except Exception as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc)}


def _sub_output_targets(runtime_config: dict, *, output_key: str, hardware_ports: list) -> dict:
    """Expected (engine port, hardware port) edge per sub side for the dump.

    Engine output ports follow the plan layout, not the hardware channel
    order: with crossover ways the sub signals can sit on any index (e.g.
    signals 5/6 for the hardware ports 3/4 in a 2-way layout).  Deriving the
    edge from the plan keeps the dump honest for every layout; contracts
    without a plan keep the historic positional guess (engine output 3/4 to
    hardware ports 3/4).
    """
    routes = {int(signal): str(port) for signal, port in (runtime_config.get("output_routes") or ())}
    layout = list(runtime_config.get("layout") or ())
    device = str(runtime_config.get("output_key") or output_key or "")
    targets = {"left": None, "right": None}
    if layout and routes and device:
        for index, channel in enumerate(layout, 1):
            role = str((channel or {}).get("role") or (channel or {}).get("name") or "")
            port = routes.get(index)
            if role in SUB_ROLES and port:
                targets[side_for_role(role)] = (f"fxroute_dsp:output_{index}", f"{device}:{port}")
        return targets
    for side, index in (("left", 3), ("right", 4)):
        if index <= len(hardware_ports) and device:
            targets[side] = (f"fxroute_dsp:output_{index}", f"{device}:{hardware_ports[index - 1]}")
    return targets


async def _dump_21_runtime_state(label: str, ui_state: dict | None = None) -> dict:
    overview = get_audio_output_overview()
    output_mode = overview.get("output_mode") or {}
    output_key = str(output_mode.get("effective_output_key") or "").strip()
    samplerate_status = get_samplerate_status()
    snapshot = runtime.dsp_runtime.snapshot() if runtime.dsp_runtime is not None else {}
    helper_pid = snapshot.get("helper_pid")
    helper_alive = False
    helper_cmdline = ""
    if helper_pid:
        ps_result = await asyncio.to_thread(_run_debug_command, ["ps", "-p", str(helper_pid), "-o", "pid=,args="], 1.5)
        helper_alive = ps_result.get("returncode") == 0 and bool(ps_result.get("stdout", "").strip())
        helper_cmdline = ps_result.get("stdout", "").strip()
    else:
        pgrep_result = await asyncio.to_thread(_run_debug_command, ["pgrep", "-af", "native_dsp/build/fxroute-dsp"], 1.5)
        helper_cmdline = pgrep_result.get("stdout", "").strip()

    pw_links = await asyncio.to_thread(_run_debug_command, ["pw-link", "-l"], 2.0)
    link_text = pw_links.get("stdout", "")

    sink_monitor_left = "fxroute_dsp_sink:monitor_FL"
    sink_monitor_right = "fxroute_dsp_sink:monitor_FR"
    dsp_in_left = "fxroute_dsp:input_1"
    dsp_in_right = "fxroute_dsp:input_2"
    dsp_out_1 = "fxroute_dsp:output_1"
    dsp_out_2 = "fxroute_dsp:output_2"
    # Read the hardware side back through the same port list the DSP links
    # against, so the dump never reports a playback_FL/FR topology the device
    # does not actually expose (e.g. playback_AUX0…).  That list is the
    # discovery-resolved one, the sink's own channel map when no port is
    # published yet, and only then the historic semantic names.
    hardware_ports = [str(port) for port in (output_mode.get("hardware_playback_ports") or ())]
    if not hardware_ports:
        hardware_ports = list(hardware_playback_port_fallback_from_mode(output_mode))
    if not hardware_ports:
        hardware_ports = ["playback_FL", "playback_FR", "playback_RL", "playback_RR"]
    hw_targets = [f"{output_key}:{port}" for port in hardware_ports[:4]] if output_key else []
    hw_fl = hw_targets[0] if len(hw_targets) > 0 else ""
    hw_fr = hw_targets[1] if len(hw_targets) > 1 else ""
    sub_targets = _sub_output_targets(snapshot.get("config") or {},
                                     output_key=output_key, hardware_ports=hardware_ports)
    links = {
        "sink_to_dsp_left": _contains_link(link_text, sink_monitor_left, dsp_in_left),
        "sink_to_dsp_right": _contains_link(link_text, sink_monitor_right, dsp_in_right),
        "dsp_main_left_to_hw": bool(hw_fl) and _contains_link(link_text, dsp_out_1, hw_fl),
        "dsp_main_right_to_hw": bool(hw_fr) and _contains_link(link_text, dsp_out_2, hw_fr),
        "dsp_sub_left_to_hw": bool(sub_targets["left"]) and _contains_link(link_text, *sub_targets["left"]),
        "dsp_sub_right_to_hw": bool(sub_targets["right"]) and _contains_link(link_text, *sub_targets["right"]),
        "direct_source_left_to_hw": bool(hw_fl) and any(
            _contains_link(link_text, f"{node}:output_FL", hw_fl) for node in ("mpv", "spotify")
        ),
        "direct_source_right_to_hw": bool(hw_fr) and any(
            _contains_link(link_text, f"{node}:output_FR", hw_fr) for node in ("mpv", "spotify")
        ),
    }
    links["sink_to_dsp_present"] = links["sink_to_dsp_left"] and links["sink_to_dsp_right"]
    links["dsp_main_to_hw_present"] = links["dsp_main_left_to_hw"] and links["dsp_main_right_to_hw"]
    links["dsp_sub_to_hw_present"] = links["dsp_sub_left_to_hw"] and links["dsp_sub_right_to_hw"]
    links["sub_output_channel_linked"] = links["dsp_sub_left_to_hw"] or links["dsp_sub_right_to_hw"]
    links["direct_source_to_hw_present"] = links["direct_source_left_to_hw"] or links["direct_source_right_to_hw"]

    config = snapshot.get("config") or {}
    state = {
        "label": label,
        "build_id": _read_build_id(),
        "api_mode": output_mode.get("mode"),
        "ui_state": ui_state or {},
        "helper_pid": helper_pid,
        "helper_alive": helper_alive,
        "helper_sample_rate": config.get("sample_rate"),
        "helper_cmdline": helper_cmdline,
        "hardware_output": output_key,
        "hardware_playback_sample_rate": output_mode.get("effective_output_rate") or samplerate_status.get("active_rate"),
        "samplerate": {
            "active_rate": samplerate_status.get("active_rate"),
            "force_rate": samplerate_status.get("force_rate"),
        },
        "links": links,
        "runtime": _measurement_helper_snapshot_summary(snapshot),
    }
    logger.info("Subwoofer UI path state dump [%s]: %s", label, json.dumps(state, sort_keys=True))
    return state


def _make_canonical_volume_deps() -> canonical_volume.CanonicalVolumeDeps:
    """Bind the canonical master volume to the application services.

    All entries resolve the current runtime state at call time, so tests that
    patch main attributes observe the patched services through the thin
    wrappers below (same contract as the other ``configure_*`` extractions).
    ``get_output_volume_safe`` deliberately resolves through main so the
    established patch seam keeps working.
    """
    return canonical_volume.CanonicalVolumeDeps(
        get_player_instance=lambda: runtime.player_instance,
        set_output_volume=lambda value: set_output_volume(value),
        get_status_volume=lambda default=100: get_status_volume(default),
        get_output_volume_safe=lambda default=100: get_output_volume_safe(default),
        drain_worker=lambda *args, **kwargs: _drain_worker(*args, **kwargs),
        canonical_volume_write_lock=lambda: _canonical_volume_write_lock(),
        resolve_playback_owner=lambda: _resolve_playback_owner(),
    )


def ensure_local_source_volume() -> None:
    """Thin wrapper: canonical master volume lives in audio.canonical_volume (REFACTOR-017)."""
    return canonical_volume.ensure_local_source_volume()


def get_output_volume_safe(default: int = 100) -> int:
    """Thin wrapper: canonical master volume lives in audio.canonical_volume (REFACTOR-017)."""
    return canonical_volume.get_output_volume_safe(default)


async def _set_canonical_output_volume(volume: float | int) -> dict[str, Any]:
    """Thin wrapper: canonical master volume lives in audio.canonical_volume (REFACTOR-017)."""
    return await canonical_volume._set_canonical_output_volume(volume)


async def _apply_remote_volume_value(
    volume_percent: int,
    *,
    owner: str | None = None,
    source_active: Callable[[], bool] | None = None,
) -> None:
    """Thin wrapper: canonical master volume lives in audio.canonical_volume (REFACTOR-017)."""
    await canonical_volume._apply_remote_volume_value(
        volume_percent, owner=owner, source_active=source_active)


async def _render_effects_transition_targets(previous, candidate):
    """Render old/new v2 plan targets for a global-extras transition.

    Returns (new_target, old_target) for the committed head at the live
    rate, or None when the head cannot activate (the caller keeps the
    legacy overview rebuild).  Never raises.
    """
    try:
        service = get_output_service()
        overview = await asyncio.to_thread(get_audio_output_overview)
        try:
            output_key, channels = _output_state_device(overview)
        except HTTPException:
            return None
        if not channels:
            return None
        status = get_samplerate_status()
        rate = status.get("active_rate")
        if not isinstance(rate, int) or rate <= 0:
            rate = status.get("force_rate")
        if not isinstance(rate, int) or rate <= 0:
            return None
        ports = list((overview.get("output_mode") or {}).get("hardware_playback_ports") or [])
        if not ports:
            return None
        manager = _require_dsp_manager()
        state = service.load()
        plan = service.compile_plan(state, output_key=output_key, channels=channels,
                                    sample_rate_hz=rate)
        fingerprint = service.fingerprint_plan(plan)
        new_target = _build_plan_target(
            service, manager, plan, output_key=output_key, rate=rate,
            hardware_ports=list(ports), fingerprint=fingerprint,
            extras_override=candidate)
        old_target = _build_plan_target(
            service, manager, plan, output_key=output_key, rate=rate,
            hardware_ports=list(ports), fingerprint=fingerprint,
            extras_override=previous)
        return new_target, old_target
    except Exception as exc:
        logger.info("Effects transition uses legacy rebuild (v2 head unavailable): %s", exc)
        return None


async def _guarded_effects_transition(previous, candidate, persist_all_presets):
    """Apply extras through a guarded DSP rebuild without touching the master."""
    overview = get_audio_output_overview()
    result_holder = {}
    start = await _volume_state_for_manager(dsp_manager)
    candidate_loudness = (candidate.get("loudness") or {})
    if start.loudness_in_path or volume_contract.loudness_in_path(
        start.preset, bool(candidate_loudness.get("enabled"))
    ):
        guard_db = dsp_manager.loudness_transition_guard_db(previous, candidate)
    else:
        guard_db = min(0.0, start.dsp_guard_db, volume_percent_to_db(start.master_percent))
    settle = float(getattr(dsp_manager, "LOUDNESS_STRENGTH_VOLUME_SETTLE_SECONDS", 0.0) or 0.0)

    def persist_candidate():
        result_holder["result"] = (
            dsp_manager.apply_global_extras_to_all_presets(candidate)
            if persist_all_presets else
            dsp_manager.apply_global_extras_to_active_preset(candidate))

    targets = await _render_effects_transition_targets(previous, candidate)
    if targets is None:
        await runtime.dsp_runtime.guarded_rebuild(
            overview,
            guard_db=guard_db,
            apply_candidate=persist_candidate,
            apply_previous=lambda: dsp_manager.save_global_extras(previous),
            settle_seconds=settle,
            candidate_extras=candidate,
            previous_extras=previous,
        )
    else:
        new_target, old_target = targets
        await runtime.dsp_runtime.guarded_rebuild_rendered(
            new_target, previous=old_target, guard_db=guard_db,
            apply_candidate=persist_candidate,
            apply_previous=lambda: dsp_manager.save_global_extras(previous),
            settle_seconds=settle)
    result = result_holder["result"]
    result["runtime_applied"] = True
    return result


async def get_spotify_ui_state(data: Optional[dict] = None) -> dict:
    status = dict(data or await spotify_get_status())
    source_volume = status.get("volume") if isinstance(status.get("volume"), (int, float)) else None
    status["source_volume"] = int(round(float(source_volume))) if source_volume is not None else None
    status["volume"] = get_output_volume_safe()
    status["playback_owner"] = _resolve_playback_owner()
    art_url = str(status.get("artwork_url") or status.get("artUrl") or "").strip()
    status["artwork_available"] = bool(art_url)
    status["artwork_url"] = art_url or None
    status["artwork_source"] = "spotify" if art_url else "none"
    return status


async def get_qobuz_ui_state(data: Optional[dict] = None) -> dict:
    """Return the normalized qbzd provider state (owner is never derived from
    a status read; it is attached for the UI payload only).

    The UI volume is the canonical FXRoute master: qbzd's engine volume stays
    pinned at 100% (Unity) while the phone slider drives only the master, so
    the raw qbzd value is reported separately as ``source_volume``.
    """
    provider = streaming.get_provider("qobuz")
    status = dict(data or await provider.status())
    source_volume = status.get("volume") if isinstance(status.get("volume"), (int, float)) else None
    status["source_volume"] = int(round(float(source_volume))) if source_volume is not None else None
    status["volume"] = get_output_volume_safe()
    status["qobuz_unity_pin"] = (
        "ok" if qobuz_unity_pin_state.get("ok") else "error"
    ) if qobuz_unity_pin_state else None
    status["playback_owner"] = _resolve_playback_owner()
    playback_state.latest_qobuz_state = status
    return status


# Health state of the last qbzd unity pin (precondition of the volume
# architecture: qbzd must stay at 100% while the phone slider drives the
# FXRoute master). Exposed as ``qobuz_unity_pin`` in the Qobuz UI state; a
# failed pin must be visible and is retried by the next claim/UI-start pin.
qobuz_unity_pin_state: dict | None = None


async def _qobuz_pin_unity() -> None:
    """Pin qbzd's engine gain to 100% (Unity).

    In ``volume_mode=locked`` the local control plane still accepts volume
    writes while remote Connect SetVolume is ignored, so this is the single
    write that keeps qbzd from attenuating; every user-facing volume input
    (phone slider via journal watch, FXRoute web slider) drives the master.

    A failed pin is not treated as harmless: it is recorded in
    ``qobuz_unity_pin_state`` (health flag surfaced in the Qobuz UI state,
    ownership stays untouched) and retried on the next pin call.
    """
    global qobuz_unity_pin_state
    provider = streaming.get_provider("qobuz")
    try:
        await provider.set_volume(100)
    except Exception as exc:
        qobuz_unity_pin_state = {"ok": False, "error": str(exc), "at": time.time()}
        logger.error(
            "Qobuz unity pin failed: %s (volume_mode must stay 'locked' so the "
            "phone slider drives the FXRoute master, not qbzd gain)",
            exc,
        )
        return
    qobuz_unity_pin_state = {"ok": True, "at": time.time()}


async def _qobuz_volume_action(percent: float) -> dict:
    """Apply the Qobuz UI slider as the canonical FXRoute master volume.

    qbzd's engine volume stays pinned at 100%; the slider drives only the
    global master.
    """
    try:
        volume_result = await _set_canonical_output_volume(percent)
    except SystemVolumeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to set output volume: {exc}")
    data = await get_qobuz_ui_state()
    data["volume"] = volume_result["volume"]
    playback_state.latest_qobuz_state = data
    await peak_monitor_coordinator.sync_qobuz_state(data)
    await manager.broadcast({"type": "qobuz", "data": data})
    return data


async def _spotify_volume_action(percent: float) -> dict:
    """Apply the Spotify UI slider as the canonical FXRoute master volume.

    spotifyd runs with ``volume_controller = "none"``: its Connect volume is a
    reported value only and never attenuates the source; the slider drives
    only the global master, mirroring the Qobuz contract.
    """
    try:
        volume_result = await _set_canonical_output_volume(percent)
    except SystemVolumeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to set output volume: {exc}")
    data = await get_spotify_ui_state()
    data["volume"] = volume_result["volume"]
    playback_state.latest_spotify_state = data
    await peak_monitor_coordinator.sync_spotify_state(data)
    return await broadcast_spotify_state(data)


async def qobuz_play() -> dict:
    provider = streaming.get_provider("qobuz")
    data = await provider.play()
    playback_state.latest_qobuz_state = data
    return data


async def qobuz_pause() -> dict:
    provider = streaming.get_provider("qobuz")
    data = await provider.pause()
    playback_state.latest_qobuz_state = data
    return data


def _qobuz_target_track_from_state(state: Mapping[str, Any]) -> dict[str, Any]:
    track_id = state.get("trackId") or state.get("id")
    return {
        "source": "qobuz",
        "id": track_id,
        "url": track_id,
        "title": state.get("title"),
        "artist": state.get("artist"),
        "album": state.get("album"),
        "artUrl": state.get("artUrl"),
        "sample_rate_hz": state.get("sample_rate"),
    }


# Degraded-state instrumentation for the Qobuz footer quality path. Off by
# default; enable with FXROUTE_DEBUG_QOBUZ_FOOTER_META=1 to log owner/source
# and the quality fields on every transition.
_qobuz_footer_meta_log_enabled = os.environ.get("FXROUTE_DEBUG_QOBUZ_FOOTER_META") == "1"
_qobuz_footer_meta_log_state: tuple | None = None


def _qobuz_footer_meta_log(data: Mapping[str, Any]) -> None:
    """Log the Qobuz footer quality fields whenever they change.

    Only active with ``FXROUTE_DEBUG_QOBUZ_FOOTER_META=1``. Deduplicated on
    (track, audio_format, bit_depth, bitrate, sample_rate) so the 2s qbzd
    watch loop does not spam the journal; only transitions (a field appearing,
    disappearing or changing value) are emitted. This is the instrumentation
    that identifies a degrading update path at the state boundary.
    """
    if not _qobuz_footer_meta_log_enabled:
        return
    global _qobuz_footer_meta_log_state
    try:
        signature = (
            data.get("trackId"),
            data.get("audio_format"),
            data.get("bit_depth"),
            data.get("bitrate"),
            data.get("sample_rate"),
        )
    except Exception:
        return
    if signature == _qobuz_footer_meta_log_state:
        return
    _qobuz_footer_meta_log_state = signature
    logger.info(
        "qobuz-footer-meta owner=%s source=%s track=%s audio_format=%s "
        "bit_depth=%s bitrate=%s sample_rate=%s",
        data.get("playback_owner"),
        data.get("source"),
        data.get("trackId"),
        data.get("audio_format"),
        data.get("bit_depth"),
        data.get("bitrate"),
        data.get("sample_rate"),
    )


async def broadcast_qobuz_state(data=None):
    data = await get_qobuz_ui_state(data)
    playback_state.latest_qobuz_state = data
    # Degraded-state instrumentation (see _qobuz_footer_meta_log): a path that
    # drops provider facts (audio_format/bit_depth/sample_rate) is identifiable
    # in the journal with FXROUTE_DEBUG_QOBUZ_FOOTER_META=1.
    _qobuz_footer_meta_log(data)
    await peak_monitor_coordinator.sync_qobuz_state(data)
    await manager.broadcast({"type": "qobuz", "data": data})
    return data


def _qobuz_target_rate(qobuz_state: Mapping[str, Any]) -> int:
    source_rate = qobuz_state.get("sample_rate")
    if isinstance(source_rate, int) and source_rate > 0:
        return samplerate.effective_playback_rate(source_rate)
    return samplerate.effective_playback_rate(44100)


async def _claim_qobuz_playback(detail: str = "qobuz-claim") -> dict:
    """Claim FXRoute playback ownership for an already-playing qbzd renderer.

    Runs a single Coordinator transition that pauses the previous source(s),
    establishes the qbzd rate/graph and commits ``playback_owner=qobuz``.
    Paused/stopped qbzd states never claim.

    The no-op guard checks the *committed* owner, not the display resolver:
    the read-only derived owner is only a fallback for display and must never
    suppress the commit that makes the owner persist across a pause.
    """
    claim_intent_generation = playback_state.playback_intent_generation
    claim_source_intent = playback_state.capture_source_intent()
    if playback_state.current_playback_owner == "qobuz":
        return await get_qobuz_ui_state()
    qobuz_state = await get_qobuz_ui_state()
    if not _is_qobuz_playback_active(qobuz_state):
        return qobuz_state

    async def skip_if_owner_committed() -> bool:
        # Same re-validation contract as the Spotify claim: the guard above
        # runs before the transition lock, so a claim queued behind an
        # FXRoute-initiated Qobuz start must be re-checked inside the lock.
        # A source switch invalidates the claim the same way.
        return (
            playback_state.current_playback_owner == "qobuz"
            or playback_state.playback_intent_generation != claim_intent_generation
            or not playback_state.source_intent_is_current(claim_source_intent)
        )

    track = _qobuz_target_track_from_state(qobuz_state)
    target_rate = _qobuz_target_rate(qobuz_state)
    rate_change = await asyncio.to_thread(_coordinator_rate_change, target_rate)
    request = TransitionRequest(
        operation="qobuz-claim",
        source="qobuz",
        target_rate=target_rate,
        target_url=str(track.get("id") or ""),
        target_track=track,
        should_play=True,
        rate_change=rate_change,
        reload_source=False,
        detail=detail,
        skip_if_committed_owner=skip_if_owner_committed,
    )
    try:
        result = await _run_coordinated_transition(request)
    except (ValueError, PlaybackTransitionFailure) as exc:
        logger.warning("Qobuz claim transition failed: %s", getattr(exc, "detail", exc) or exc)
        return qobuz_state
    if (getattr(result, "state", {}) or {}).get("skipped"):
        return await get_qobuz_ui_state()
    if not getattr(result, "committed", False):
        return qobuz_state
    if not playback_state.source_intent_is_current(claim_source_intent):
        return await get_qobuz_ui_state()
    await _publish_committed_playback_owner("qobuz", getattr(result, "transition_id", None))
    connect_state.set_device_active(True)
    await _qobuz_pin_unity()
    return await broadcast_qobuz_state()


async def _claim_spotify_playback(detail: str = "spotify-claim") -> dict:
    """Claim FXRoute playback ownership for an already-playing Spotify renderer.

    Triggered by the MPRIS watcher on a real Playing event (Spotify Connect
    started playback on another device), independent of the visible tab.

    The no-op guard checks the *committed* owner, not the display resolver:
    the read-only derived owner is only a fallback for display and must never
    suppress the commit that makes the owner persist across a pause.
    """
    spotifyd_volume_watch.reset_session()
    claim_intent_generation = playback_state.playback_intent_generation
    claim_source_intent = playback_state.capture_source_intent()
    if playback_state.current_playback_owner == "spotify":
        return await get_spotify_ui_state()
    data = await get_spotify_ui_state()
    if not _is_spotify_playback_active(data):
        return data

    async def skip_if_owner_committed() -> bool:
        # The guard above runs before the transition lock, so a claim queued
        # behind an FXRoute-initiated Spotify start would close the output
        # gate a second time over already-audible audio.  The Coordinator
        # re-validates inside the lock; the initiating start commits the
        # owner synchronously before the queued claim can acquire it.
        # A source switch invalidates the claim the same way.
        return (
            playback_state.current_playback_owner == "spotify"
            or playback_state.playback_intent_generation != claim_intent_generation
            or not playback_state.source_intent_is_current(claim_source_intent)
        )

    target_rate = _coordinator_target_rate("spotify")
    rate_change = await asyncio.to_thread(_coordinator_rate_change, target_rate)
    request = TransitionRequest(
        operation="spotify-claim",
        source="spotify",
        target_rate=target_rate,
        should_play=True,
        rate_change=rate_change,
        reload_source=True,
        detail=detail,
        skip_if_committed_owner=skip_if_owner_committed,
    )
    try:
        result = await _run_coordinated_transition(request)
    except (ValueError, PlaybackTransitionFailure) as exc:
        logger.warning("Spotify claim transition failed: %s", getattr(exc, "detail", exc) or exc)
        return data
    if (getattr(result, "state", {}) or {}).get("skipped"):
        return await get_spotify_ui_state()
    if not getattr(result, "committed", False):
        return data
    if not playback_state.source_intent_is_current(claim_source_intent):
        return await get_spotify_ui_state()
    await _publish_committed_playback_owner("spotify", getattr(result, "transition_id", None))
    return await broadcast_spotify_state()


def _radio_artwork_url_for_track(track: dict) -> str:
    station_id = str(track.get("station_id") or track.get("id") or "")
    if station_id.startswith("radio_"):
        station_id = station_id[len("radio_"):]
    if not station_id:
        return ""
    try:
        for station in get_stations():
            if station.id == station_id:
                return _station_api_payload(station).get("image") or ""
    except Exception as exc:
        logger.debug("Failed to resolve radio artwork for %s: %s", station_id, exc)
    return ""


def _playback_track_with_artwork_fields(track_info: Optional[dict]) -> Optional[dict]:
    if not track_info:
        return None
    track = dict(track_info)
    track_id = str(track.get("id") or "")
    source = track.get("source")
    if source == "radio":
        artwork_url = _radio_artwork_url_for_track(track)
        track["artwork_available"] = bool(artwork_url)
        track["artwork_url"] = artwork_url or None
        track["artwork_source"] = "radio" if artwork_url else "none"
        return track
    if source == "tidal":
        art_url = str(track.get("art_url") or track.get("artUrl") or "").strip()
        track["artwork_available"] = bool(art_url)
        track["artwork_url"] = art_url or None
        track["artwork_source"] = "tidal" if art_url else "none"
        return track
    if source != "local" or not track_id:
        track["artwork_available"] = False
        track["artwork_url"] = None
        track["artwork_source"] = "none"
        return track
    try:
        cover_available = bool(runtime.music_library.scanner and _track_cover_available(track_id))
    except Exception as exc:
        logger.debug("Failed to resolve playback cover availability for %s: %s", track_id, exc)
        cover_available = False
    encoded_id = quote(track_id, safe="")
    track["cover_available"] = cover_available
    track["cover_info_url"] = f"/api/tracks/cover-info/{encoded_id}"
    if cover_available:
        track["cover_url"] = f"/api/tracks/cover/{encoded_id}"
    track["artwork_available"] = cover_available
    track["artwork_url"] = track.get("cover_url") if cover_available else None
    track["artwork_source"] = "library" if cover_available else "none"
    return track


def build_playback_payload(
    state: Optional[dict] = None,
    *,
    include_live_metadata: bool = True,
) -> dict:
    global dsp_manager
    player_state = dict(state or (runtime.player_instance.state if runtime.player_instance else {}))
    source_volume = player_state.get("volume") if isinstance(player_state.get("volume"), (int, float)) else None
    if playback_state.current_track_info and source_policy.is_mpv_source(playback_state.current_track_info.get("source")):
        player_state["source_volume"] = int(round(float(source_volume))) if source_volume is not None else None
    elif source_volume is not None:
        player_state["source_volume"] = int(round(float(source_volume)))
    player_state["volume"] = get_output_volume_safe()
    # Radio: hide stale track from UI when mpv has no active stream.
    # Prevents UI showing a resumable station when the stream connection
    # is dead and mpv is idle (current_file=None, ended=True).
    _effective_track = playback_state.current_track_info
    if _effective_track and _effective_track.get("source") == "radio":
        cur_file = player_state.get("current_file")
        if not cur_file or player_state.get("ended"):
            _effective_track = None
    player_state["current_track"] = _playback_track_with_artwork_fields(_effective_track)
    player_state["queue"] = playback_queue.queue.payload()
    player_state["playback_owner"] = _resolve_playback_owner()

    live_title = None
    if include_live_metadata and runtime.player_instance and playback_state.current_track_info and playback_state.current_track_info.get("source") == "radio":
        metadata = runtime.player_instance.get_metadata() if player_state.get("current_file") else {}
        title = (metadata.get("icy-title") or metadata.get("title") or "").strip()
        if title:
            live_title = title
        player_state["metadata"] = metadata

    player_state["live_title"] = live_title
    player_state["output_peak_warning"] = runtime.peak_monitor.snapshot() if runtime.peak_monitor else {
        "available": False,
        "detected": False,
        "hold_ms": 0,
        "threshold": 1.0,
        "vu_db": None,
        "vu_db_l": None,
        "vu_db_r": None,
        "detected_l": False,
        "detected_r": False,
        "hold_ms_l": 0,
        "hold_ms_r": 0,
        "vu_fresh": False,
        "vu_age_ms": None,
        "target": None,
        "last_over_at": None,
        "last_over_at_l": None,
        "last_over_at_r": None,
        "last_error": None,
    }
    if playback_transition_coordinator:
        transition_status = playback_transition_coordinator.status()
        player_state["transition"] = transition_status
        gate = transition_status.get("gate") or {}
        if transition_status.get("transition_blocked"):
            # A physical source may still report playing while FXRoute owns a
            # safety mute, or while the coordinator has not yet returned a
            # committed result.  Do not expose that transient as committed
            # normal playback to the UI; the structured transition status is
            # the authoritative state until the gate is released.
            player_state["playing"] = False
            player_state["safe_muted"] = True
            player_state["transition_status"] = (
                "failure-latched"
                if gate.get("failure_latched")
                else "transitioning" if transition_status.get("active") else "safe-muted"
            )

    # Keep playback/status payloads lightweight. The DSP has dedicated
    # endpoints and websocket updates, and pulling full DSP status here
    # can stall frequent /api/status polling during playback.
    return player_state


async def _read_status_player_detail(reader: Callable[[], Any], default: Any) -> Any:
    """Keep optional MPV telemetry off the asyncio event loop and bounded."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(reader), timeout=1.0)
    except Exception as exc:
        logger.debug("Status MPV telemetry read failed: %s", exc)
        return default


async def on_peak_monitor_change(snapshot: dict):
    await manager.broadcast({"type": "playback_peak_warning", "data": snapshot})


# Callback functions
def _mark_player_state_authoritative(state: dict | None) -> None:
    seq = (state or {}).get("_seq")
    if isinstance(seq, int):
        playback_state.latest_player_state_seq_seen = max(playback_state.latest_player_state_seq_seen, seq)


def _dispatch_player_state_change(state: dict):
    """Synchronous player-state dispatcher.

    playback/player.py invokes this at notify time (before any coroutine task is
    queued) and schedules the returned coroutine as a task.  Capturing the
    committed playback token here instead of inside the async callback binds
    the state event to the playback context that was committed when the
    event was observed: a stale end-file event keeps its original token even
    when its callback task runs after a newer commit.
    """
    return on_player_state_change(
        state,
        event_commit_id=_current_playback_commit_id(),
    )


async def on_player_state_change(state: dict, event_commit_id: str | None = None):
    global queue_advancing
    callback_generation = _capture_playback_transition_epoch()
    seq = state.get("_seq")
    if isinstance(seq, int):
        if seq < playback_state.latest_player_state_seq_seen:
            return
        playback_state.latest_player_state_seq_seen = seq

    # Ended ownership: an end-file event may only mutate queue or playback
    # while it still belongs to the currently committed playback context.
    # The event's captured commit token must equal the current successful
    # Coordinator commit.  While a transition attempt is in flight the
    # callback waits for it to settle first: a failed attempt leaves the
    # committed token unchanged and this EOF stays legitimate; a committed
    # attempt replaces the token and the stale EOF becomes a no-op.
    if state.get("ended") and not state.get("current_file"):
        await _wait_playback_transition_settled()
        current_commit_id = _current_playback_commit_id()
        if event_commit_id != current_commit_id:
            logger.debug(
                "Stale ended player event discarded: seq=%s entry_id=%s event_commit_id=%s current_commit_id=%s",
                seq,
                state.get("end_entry_id"),
                event_commit_id,
                current_commit_id,
            )
            return

    # Once a homogeneous queue has been committed, MPV owns natural playlist
    # boundaries.  The queue module mirrors MPV's position into the committed
    # queue index; this callback only applies the app-side track context and
    # must never start another rate, DSP, graph or gate transition.
    synced = playback_queue.queue.sync_index_from_mpv(state)
    if synced is not None:
        queue_index, track = synced
        previous_track = playback_state.current_track_info or {}
        playback_state.current_track_info = track
        playback_state.last_track_info = track
        if (
            previous_track.get("source") != track.get("source")
            or previous_track.get("id") != track.get("id")
            or previous_track.get("url") != track.get("url")
        ):
            _mark_playback_intent_changed()

    if (
        not queue_advancing
        and state.get("ended")
        and not state.get("current_file")
        and playback_state.current_track_info
        and playback_state.current_track_info.get("source") in {"local", "tidal"}
        and playback_queue.queue.mode != "native_mpv"
    ):
        queue_advancing = True
        try:
            if len(playback_queue.queue.tracks) > 1 and await playback_queue.queue.advance(transition_reason="queue auto-advance") == "advanced":
                return
            if playback_queue.queue.single_track_loop and playback_state.current_track_info and playback_state.current_track_info.get("url"):
                loop_track = dict(playback_state.current_track_info)
                loop_rate = _coordinator_target_rate("local", loop_track)
                loop_rate_change = await asyncio.to_thread(
                    _coordinator_rate_change, loop_rate
                )
                try:
                    result = await _run_coordinated_transition(TransitionRequest(
                        operation="replay",
                        source="local",
                        target_rate=loop_rate,
                        target_url=loop_track.get("url"),
                        target_track=loop_track,
                        should_play=True,
                        rate_change=loop_rate_change,
                        reload_source=True,
                        detail="single-track-loop",
                    ))
                    if _sample_rate_policy_is_auto() and isinstance(result.target_rate, int) and result.target_rate > 0:
                        playback_state.current_track_info["sample_rate_hz"] = result.target_rate
                    # A new physical playback instance was committed:
                    # publish only the playback instance token (no
                    # _commit_coordinated_track: its side effects like
                    # intent/history are unwanted for automatic single-track
                    # loop).
                    _publish_playback_context_commit(getattr(result, "transition_id", None))
                except PlaybackTransitionFailure as exc:
                    logger.warning("Single-track loop transition failed: %s", exc.as_status())
                return
        finally:
            queue_advancing = False

    radio_reconnect.schedule(state)
    if runtime.source_transition_lock is None:
        await peak_monitor_coordinator.sync_playback_state(state, callback_generation)
    else:
        # Serialize callback context application with explicit play handoffs.
        # A callback queued before/during a handoff observes an obsolete
        # generation after acquiring the lock and becomes a no-op.
        async with runtime.source_transition_lock:
            await peak_monitor_coordinator.sync_playback_state(state, callback_generation)
    if not _playback_transition_context_is_current(callback_generation):
        logger.debug(
            "Discarding stale player callback after playback transition: callback_generation=%s current_generation=%s",
            callback_generation,
            playback_state.playback_transition_epoch,
        )
        return
    await manager.broadcast({"type": "playback", "data": build_playback_payload(state)})

async def on_download_progress(progress):
    data = progress.to_dict() if hasattr(progress, "to_dict") else progress
    await manager.broadcast({"type": "download", "data": data})

    status = (data or {}).get("status")
    if status == "complete":
        if runtime.music_library.scanner:
            await _drain_worker(runtime.music_library.scanner.refresh, True, wait_if_running=True)
        await manager.broadcast({"type": "download_complete", "data": data})
    elif status == "error":
        await manager.broadcast({"type": "download_error", "data": data})

async def broadcast_spotify_state(data=None):
    data = await get_spotify_ui_state(data)
    playback_state.latest_spotify_state = data
    await peak_monitor_coordinator.sync_spotify_state(data)
    if _is_spotify_playback_active(data):
        signature_payload = repr(_spotify_state_signature(data)).encode("utf-8", errors="replace")
        silent_active_recovery.schedule(
            source="spotify",
            signature=f"spotify:{hashlib.sha1(signature_payload).hexdigest()}",
            spotify_state=data.copy(),
        )
    await manager.broadcast({"type": "spotify", "data": data})
    return data


def _spotify_state_signature(data: Optional[dict]) -> tuple:
    data = data or {}
    duration = data.get("duration")
    try:
        duration_key = round(float(duration or 0), 3)
    except (TypeError, ValueError):
        duration_key = 0.0
    return (
        data.get("status") or "",
        data.get("trackId") or data.get("trackid") or "",
        data.get("title") or "",
        data.get("artist") or "",
        data.get("album") or "",
        data.get("artUrl") or "",
        duration_key,
        bool(data.get("available")),
        bool(data.get("installed")),
    )


def _spotify_identity_signature(data: Optional[dict]) -> tuple:
    return _spotify_state_signature(data)[1:]


def _spotify_refresh_should_broadcast(new_state: dict, old_state: Optional[dict]) -> bool:
    if old_state is None:
        return bool(new_state.get("available") and (new_state.get("status") == "Playing" or new_state.get("title")))
    return _spotify_state_signature(new_state) != _spotify_state_signature(old_state)


async def _refresh_spotify_state_from_mpris(reason: str, *, force: bool = False) -> None:
    try:
        data = await get_spotify_ui_state()
        if force or _spotify_refresh_should_broadcast(data, playback_state.latest_spotify_state):
            if _spotify_identity_signature(data) != _spotify_identity_signature(playback_state.latest_spotify_state):
                logger.info(
                    "Spotify metadata refresh: reason=%s status=%s title=%s artist=%s trackId=%s",
                    reason,
                    data.get("status"),
                    data.get("title"),
                    data.get("artist"),
                    data.get("trackId"),
                )
            await broadcast_spotify_state(data)
        else:
            playback_state.latest_spotify_state = data
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Spotify metadata refresh failed (%s): %s", reason, exc)


def _schedule_spotify_state_refresh(reason: str) -> None:
    if runtime.spotify_state_refresh_task and not runtime.spotify_state_refresh_task.done():
        runtime.spotify_state_refresh_task.cancel()

    async def _delayed_refresh() -> None:
        await asyncio.sleep(SPOTIFY_STATE_REFRESH_DEBOUNCE_SECONDS)
        await _refresh_spotify_state_from_mpris(reason)

    runtime.spotify_state_refresh_task = asyncio.create_task(
        _delayed_refresh(),
        name="spotify-state-refresh",
    )


async def _spotify_state_poll_loop() -> None:
    logger.info("Spotify metadata poll fallback entered")
    while True:
        try:
            await _refresh_spotify_state_from_mpris("poll-fallback")
            state = playback_state.latest_spotify_state or {}
            active = bool(state.get("available") and (state.get("status") == "Playing" or playback_state.current_playback_owner == "spotify"))
            await asyncio.sleep(SPOTIFY_STATE_POLL_INTERVAL_SECONDS if active else SPOTIFY_STATE_IDLE_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Spotify metadata poll fallback failed: %s", exc)
            await asyncio.sleep(SPOTIFY_STATE_IDLE_POLL_INTERVAL_SECONDS)


async def _spotify_player_present(timeout: float = 0.8) -> bool:
    """Return whether any Spotify backend player is running (desktop or spotifyd)."""
    try:
        players = await spotify_mpris.list_players(timeout=timeout)
        return spotify_mpris.SPOTIFY_DESKTOP_PLAYER in players or any(
            spotify_mpris.is_spotifyd_player(player) for player in players
        )
    except Exception:
        return False


async def pause_spotify_for_local_playback_broadcast():
    if not await _spotify_player_present():
        playback_state.latest_spotify_state = {
            "available": playerctl_available(),
            "installed": spotify_installed(),
            "source": "spotify",
            "status": "Stopped",
            "playback_owner": None,
        }
        await manager.broadcast({"type": "spotify", "data": playback_state.latest_spotify_state})
        return
    try:
        import shutil
        pc = shutil.which("playerctl")
        if pc:
            player = await spotify_mpris.resolve_player_name(await spotify_mpris.detect_backend())
            proc = await asyncio.create_subprocess_exec(pc, f"--player={player}", "pause")
            await asyncio.wait_for(proc.communicate(), timeout=3)
    except Exception as exc:
        logger.warning("Spotify pause for local playback failed: %s", exc)
    try:
        await broadcast_spotify_state()
    except Exception as exc:
        logger.warning("Spotify state broadcast after local pause failed: %s", exc)


async def pause_local_playback_for_spotify_broadcast():
    try:
        if runtime.player_instance and runtime.player_instance._running:
            await _drain_worker(runtime.player_instance.stop_playback)
            playback_state.current_track_info = None
            await manager.broadcast({"type": "playback", "data": build_playback_payload(runtime.player_instance.state)})
            released = await media_readiness.wait_for_pipewire_mpv_release()
            if not released:
                await asyncio.sleep(SOURCE_HANDOFF_SETTLE_MS / 1000)
    except Exception as exc:
        logger.warning("Local pause for Spotify playback failed: %s", exc)


def _overview_sample_rate(overview: dict | None) -> int | None:
    """Thin wrapper: overview rate extraction lives in samplerate (REFACTOR-003)."""
    return samplerate.overview_sample_rate(overview)


def _authoritative_sample_rate(status: dict | None) -> int | None:
    """Thin wrapper: authoritative rate extraction lives in samplerate (REFACTOR-003)."""
    return samplerate.authoritative_sample_rate(status)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown."""
    global settings, downloader, dsp_manager, measurement_store, measurement_sr_session, hardware_controller, playback_transition_coordinator

    logger.info("Starting FXRoute... build_id=%s", _read_build_id())
    try:
        settings = get_settings()
        logger.info("Configuration loaded. MUSIC_ROOT: %s", settings.MUSIC_ROOT)
        logger.info("Download directory: %s", settings.download_dir)

        runtime.player_instance = get_player()
        try:
            await _drain_worker(runtime.player_instance.start)
            logger.info("MPV player started")
            await _drain_worker(ensure_local_source_volume)
        except MPVNotInstalledError as exc:
            logger.error("Failed to start MPV: %s", exc)

        runtime.music_library.manager = MusicLibraryManager(settings.MUSIC_ROOT)
        runtime.music_library.scanner = _library_scanner_for(runtime.music_library.manager.active_root)
        runtime.music_library.scanner.prepare_scan_status()
        runtime.library_scan_task = _create_library_refresh_task(
            runtime.music_library.scanner,
            name="initial-library-scan",
        )
        logger.info("Library scanner initialized; initial scan running in background")

        try:
            downloader = Downloader()
            logger.info("Downloader initialized")
        except Exception as exc:
            downloader = None
            logger.warning("Downloader not available: %s", exc)

        dsp_manager = await _drain_worker(DSPManager)
        volume_read_monitor_task = start_volume_read_monitor()
        runtime.lifecycle_background_tasks.add(volume_read_monitor_task)
        volume_read_monitor_task.add_done_callback(runtime.lifecycle_background_tasks.discard)
        logger.info("FXRoute DSP manager initialized")

        measurement_store = MeasurementStore()
        logger.info("Measurement store initialized: %s", measurement_store.measurements_dir)
        measurement_sr_session = MeasurementSampleRateSession()
        runtime.measurement_watchdog_task = asyncio.create_task(
            measurement_sr_session.run_watchdog(),
            name="measurement-session-watchdog",
        )
        logger.info("Measurement sample-rate session initialized")

        playback_transition_coordinator = PlaybackTransitionCoordinator(
            FxrouteTransitionRuntime(make_playback_runtime_deps()),
            gate_state_path=_playback_gate_state_path(),
            get_source_generation=lambda: playback_state.source_generation,
        )
        startup_gate_reconciled = await playback_transition_coordinator.reconcile_startup_gate()
        logger.info(
            "Playback transition startup gate reconciled: success=%s status=%s",
            startup_gate_reconciled,
            playback_transition_coordinator.status(),
        )
        logger.info("Playback transition coordinator initialized")

        if HardwareController is None:
            logger.info("Optional hardware controller module not installed")
            hardware_controller = None
        else:
            try:
                hardware_controller = HardwareController(device_path=settings.HARDWARE_CONTROLLER_DEVICE)
                logger.info("Optional hardware controller initialized")
            except Exception as exc:
                logger.warning("Hardware controller not available: %s", exc)
                hardware_controller = None

        runtime.peak_monitor = DSPPeakMonitor(on_change=on_peak_monitor_change)
        runtime.dsp_runtime = DSPRuntime(dsp_manager)
        if hasattr(measurement_store, "runtime_snapshot_provider"):
            measurement_store.runtime_snapshot_provider = getattr(runtime.dsp_runtime, "snapshot", None)
            measurement_store.effect_bypass_setter = getattr(runtime.dsp_runtime, "set_effect_bypass", None)
            measurement_store.raw_scope_enter = getattr(runtime.dsp_runtime, "enter_raw_measurement", None)
            measurement_store.raw_scope_exit = getattr(runtime.dsp_runtime, "exit_raw_measurement", None)
            measurement_store.active_scope_enter = getattr(runtime.dsp_runtime, "enter_active_measurement", None)
            measurement_store.active_scope_exit = getattr(runtime.dsp_runtime, "exit_active_measurement", None)
            measurement_store.output_mask_apply = getattr(runtime.dsp_runtime, "apply_output_mask", None)
            measurement_store.output_mask_clear = getattr(runtime.dsp_runtime, "clear_output_mask", None)
            measurement_store.measurement_target_provider = _freeze_measurement_target
        runtime_loop = asyncio.get_running_loop()

        def _sync_output_mask(method_name):
            method = getattr(runtime.dsp_runtime, method_name, None)
            if not callable(method):
                return None

            def run(mask):
                return asyncio.run_coroutine_threadsafe(method(mask), runtime_loop).result()

            return run

        # L/R repeat drives its sweeps from a synchronous worker, so it needs
        # the same mask control without an await.
        if hasattr(measurement_store, "output_mask_apply_sync"):
            measurement_store.output_mask_apply_sync = _sync_output_mask("apply_output_mask")
            measurement_store.output_mask_clear_sync = _sync_output_mask("clear_output_mask")

        def guarded_effects_transition(previous, candidate, persist_all_presets):
            return asyncio.run_coroutine_threadsafe(
                _guarded_effects_transition(previous, candidate, persist_all_presets),
                runtime_loop,
            ).result()

        dsp_manager.runtime_transition_callback = guarded_effects_transition

        def temporary_effects_transition(previous, candidate):
            async def transition():
                async with _dsp_mutation_lock():
                    targets = await _render_effects_transition_targets(previous, candidate)
                    if targets is None:
                        await runtime.dsp_runtime.guarded_rebuild(
                            get_audio_output_overview(),
                            guard_db=-18.0,
                            apply_candidate=lambda: None,
                            apply_previous=lambda: None,
                            settle_seconds=dsp_manager.LOUDNESS_STRENGTH_VOLUME_SETTLE_SECONDS,
                            candidate_extras=candidate,
                            previous_extras=previous,
                        )
                    else:
                        new_target, old_target = targets
                        await runtime.dsp_runtime.guarded_rebuild_rendered(
                            new_target, previous=old_target, guard_db=-18.0,
                            apply_candidate=lambda: None,
                            apply_previous=lambda: None,
                            settle_seconds=dsp_manager.LOUDNESS_STRENGTH_VOLUME_SETTLE_SECONDS)
            asyncio.run_coroutine_threadsafe(transition(), runtime_loop).result()

        dsp_manager.temporary_runtime_transition_callback = temporary_effects_transition
        try:
            stop_orphans = getattr(runtime.dsp_runtime, "_stop_orphan_helpers", None)
            if callable(stop_orphans):
                await stop_orphans()
        except Exception:
            pass
        peak_monitor_coordinator.reset()
        runtime.dsp_preset_load_lock = asyncio.Lock()
        runtime.source_transition_lock = asyncio.Lock()
        playback_state.latest_spotify_state = await get_spotify_ui_state()
        await peak_monitor_coordinator.sync_spotify_state(playback_state.latest_spotify_state)
        playback_state.latest_qobuz_state = await get_qobuz_ui_state()
        await peak_monitor_coordinator.sync_qobuz_state(playback_state.latest_qobuz_state)
        logger.info("DSP output peak monitor initialized")

        try:
            recovery = await asyncio.to_thread(recover_saved_output_sink)
            if recovery.get("attempted"):
                logger.info(
                    "Saved-output-sink recovery attempted: attempted=%s recovered=%s reason=%s",
                    recovery.get("attempted"), recovery.get("recovered"), recovery.get("reason"),
                )
        except Exception as exc:
            logger.warning("Saved-output-sink recovery failed: %s", exc)
        try:
            applied_output = apply_persisted_audio_output_selection()
            if applied_output and applied_output.get("selected_output"):
                logger.info("Re-applied persisted audio output selection: %s", applied_output["selected_output"].get("target_label"))
            policy = samplerate.load_sample_rate_policy()
            if policy.get("mode") == "fixed":
                try:
                    await _transition_sample_rate_policy(policy, detail="startup-sample-rate-policy")
                    logger.info("Re-applied fixed sample-rate policy: %s Hz", policy.get("rate"))
                except Exception as exc:
                    logger.warning("Failed to re-apply fixed sample-rate policy: %s", exc)
            await dsp_orchestrator.sync_runtime(applied_output or get_audio_output_overview())
            runtime.dsp_runtime_link_watch_task = asyncio.create_task(
                dsp_orchestrator.runtime_link_watch_loop(),
                name="subwoofer-runtime-link-watch",
            )
        except Exception as exc:
            logger.warning("Failed to re-apply persisted audio output selection: %s", exc)

        try:
            applied_source = get_audio_source_overview()
            applied_source = await external_input.sync(applied_source)
            applied_source = await bluetooth_input.sync(applied_source)
            if applied_source.get("mode") == SOURCE_MODE_EXTERNAL_INPUT:
                logger.info(
                    "Re-applied persisted external-input monitoring: %s",
                    ((applied_source.get("selected_input") or applied_source.get("current_input") or {}).get("label") or "unknown input"),
                )
            elif applied_source.get("mode") == SOURCE_MODE_BLUETOOTH_INPUT:
                logger.info("Re-applied persisted Bluetooth input mode")
            await peak_monitor_coordinator.sync_source_mode_state(applied_source)
        except Exception as exc:
            logger.warning("Failed to re-apply source monitoring: %s", exc)

        bluetooth_input.monitor_task = asyncio.create_task(
            bluetooth_input.run_monitor_loop(),
            name="bluetooth-input-monitor",
        )
        logger.info("Starting Spotify playerctl watch task")
        await spotify_playerctl_watch.rearm()
        logger.info("Starting Qobuz qbzd claim watch task")
        qobuz_player_watch.watch_task = asyncio.create_task(
            qobuz_player_watch.run_watch_loop(),
            name="qobuz-qbzd-claim-watch",
        )
        logger.info("Starting Qobuz qbzd journal volume watch task")
        qobuz_volume_watch.watch_task = asyncio.create_task(
            qobuz_volume_watch.run_watch_loop(),
            name="qobuz-journal-volume-watch",
        )
        logger.info("Starting spotifyd remote volume watch task")
        spotifyd_volume_watch.watch_task = asyncio.create_task(
            spotifyd_volume_watch.run_watch_loop(),
            name="spotifyd-volume-watch",
        )
        logger.info("Starting Spotify metadata poll fallback task")
        runtime.spotify_state_poll_task = asyncio.create_task(
            _spotify_state_poll_loop(),
            name="spotify-state-poll",
        )

        runtime.player_instance.register_callbacks(_dispatch_player_state_change)
        if downloader is not None:
            downloader.register_callback(on_download_progress, asyncio.get_running_loop())
        try:
            await streaming_api._sync_spotify_connect_name_best_effort()
        except Exception as exc:
            logger.warning("Spotify Connect name sync failed: %s", exc)
        logger.info("Application startup complete build_id=%s", _read_build_id())
        yield
    except asyncio.CancelledError:
        logger.warning("FXRoute startup or lifespan cancelled")
        raise
    except Exception:
        logger.exception("FXRoute startup or lifespan failed")
        raise
    finally:
        await _shutdown_lifespan_resources()


async def _shutdown_lifespan_resources() -> None:
    global settings, downloader, dsp_manager, measurement_store, measurement_sr_session, hardware_controller, playback_transition_coordinator

    async def cleanup(label: str, operation) -> None:
        nonlocal cleanup_cancelled
        try:
            result = operation()
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        cleanup_cancelled = True
                        continue
                task.result()
        except asyncio.CancelledError:
            cleanup_cancelled = True
            logger.warning("Cleanup cancellation deferred until resources are released: %s", label)
        except Exception:
            logger.exception("Cleanup failed: %s", label)

    cleanup_cancelled = False
    owned_tasks = [
        runtime.dsp_runtime_link_watch_task,
        runtime.measurement_watchdog_task,
        runtime.spotify_state_refresh_task,
        runtime.spotify_state_poll_task,
        *runtime.lifecycle_background_tasks,
    ]
    tasks = list({task for task in owned_tasks if task is not None and not task.done()})
    for task in tasks:
        task.cancel()
    if tasks:
        async def drain_background_tasks() -> None:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, result in zip(tasks, results):
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    logger.warning("Background task failed during cleanup: task=%s error=%s", task.get_name(), result)

        await cleanup("background-tasks", drain_background_tasks)
    # Watcher subsystems own their task lifecycles; stopping them cancels
    # their in-flight tasks and releases their state.
    await cleanup("spotify-watch", spotify_playerctl_watch.stop)
    await cleanup("qobuz-watch", qobuz_player_watch.stop)
    await cleanup("qobuz-volume-watch", qobuz_volume_watch.stop)
    await cleanup("spotifyd-volume-watch", spotifyd_volume_watch.stop)
    await cleanup("radio-reconnect", radio_reconnect.stop)
    await cleanup("silent-active-recovery", silent_active_recovery.stop)
    runtime.lifecycle_background_tasks.clear()

    if runtime.player_instance is not None:
        await cleanup(
            "player-callbacks",
            lambda: runtime.player_instance.shutdown_callbacks(_dispatch_player_state_change),
        )
    await cleanup("autosub", autosub.shutdown)
    if measurement_store is not None:
        if _speaker_align_service_instance is not None:
            await cleanup("speaker-align", _speaker_align_service_instance.shutdown)
        await cleanup("measurement-store", measurement_store.shutdown)
    await cleanup("spl-calibration", spl_calibration.shutdown)
    if measurement_sr_session is not None:
        await cleanup("measurement-session", measurement_sr_session.request_close)
    late_background_tasks = [task for task in runtime.lifecycle_background_tasks if not task.done()]
    for task in late_background_tasks:
        task.cancel()
    if late_background_tasks:
        await cleanup(
            "late-background-tasks",
            lambda: asyncio.gather(*late_background_tasks, return_exceptions=True),
        )
    runtime.lifecycle_background_tasks.clear()
    if downloader is not None:
        await cleanup("downloader", lambda: asyncio.to_thread(downloader.shutdown))
    if runtime.music_library.scanner is not None:
        runtime.music_library.scanner.cancel_refresh()
    refresh_tasks = [task for task in runtime.library_refresh_tasks if not task.done()]
    for task in refresh_tasks:
        task.cancel()
    if runtime.library_scan_task is not None and not runtime.library_scan_task.done():
        if runtime.library_scan_task not in refresh_tasks:
            runtime.library_scan_task.cancel()
            refresh_tasks.append(runtime.library_scan_task)
    if refresh_tasks:
        await cleanup(
            "library-refresh-tasks",
            lambda: asyncio.gather(*refresh_tasks, return_exceptions=True),
        )
    runtime.library_refresh_tasks.clear()
    if runtime.player_instance is not None:
        await cleanup("player", lambda: asyncio.to_thread(runtime.player_instance.stop))
    if runtime.dsp_runtime is not None:
        await cleanup("subwoofer-runtime", runtime.dsp_runtime.stop)
    await cleanup("bluetooth-input", bluetooth_input.stop)
    await cleanup("bluetooth-receiver", lambda: asyncio.to_thread(set_bluetooth_receiver_enabled, False))
    await cleanup("external-input", external_input.disable)
    if runtime.peak_monitor is not None:
        await cleanup("peak-monitor", runtime.peak_monitor.stop)
    if hardware_controller is not None:
        await cleanup("hardware-controller", lambda: asyncio.to_thread(hardware_controller.close))

    if not manager.is_idle:
        await cleanup("websocket-clients", manager.shutdown)

    runtime.reset()
    settings = None
    downloader = None
    dsp_manager = None
    measurement_store = None
    measurement_sr_session = None
    hardware_controller = None
    playback_transition_coordinator = None
    if cleanup_cancelled:
        raise asyncio.CancelledError

def _make_dsp_api_deps() -> dsp_api.DspApiDeps:
    """Bind the /api/dsp/* routes to the application's DSP services.

    All entries resolve the current runtime state at call time, so tests that
    patch main.runtime attributes observe the patched services.
    """
    return dsp_api.DspApiDeps(
        require_dsp_manager=lambda: _require_dsp_manager(),
        get_dsp_manager=lambda: dsp_manager,
        get_dsp_runtime=lambda: runtime.dsp_runtime,
        get_dsp_preset_load_lock=lambda: _get_dsp_preset_load_lock(),
        dsp_mutation_lock=lambda: _dsp_mutation_lock(),
        canonical_volume_write_lock=lambda: _canonical_volume_write_lock(),
        drain_worker=lambda *args, **kwargs: _drain_worker(*args, **kwargs),
        run_locked_worker=lambda *args, **kwargs: _run_locked_worker(*args, **kwargs),
        broadcast=lambda message: manager.broadcast(message),
        load_dsp_preset=lambda *args, **kwargs: _load_dsp_preset(*args, **kwargs),
        restore_volume_state=lambda *args, **kwargs: preset_loading._restore_volume_state(*args, **kwargs),
        volume_state_for_manager=lambda *args, **kwargs: _volume_state_for_manager(*args, **kwargs),
        schedule_peak_monitor_refresh=lambda reason: dsp_orchestrator.schedule_peak_monitor_refresh_after_effects_change(reason),
        get_output_service=lambda: get_output_service(),
        sync_v2_head_live=lambda: _sync_v2_head_after_bank_assign(),
        verify_measurement_commit=lambda measurement_id, binding: _verify_measurement_commit(
            measurement_id, binding),
    )


def _current_output_mode() -> str:
    """Cheap current output mode for the subwoofer link-watcher gate.

    Prefers the committed DSP runtime config, then the committed v2 output
    state (any routed sub role reads as a subwoofer mode), then stereo.  It
    never builds the PipeWire overview, so the idle link-watcher tick stays
    cheap.
    """
    dsp_runtime = runtime.dsp_runtime
    if dsp_runtime is not None:
        snapshot = dsp_runtime.snapshot()
        mode = (snapshot.get("config") or {}).get("output_mode")
        if mode:
            return str(mode)
    try:
        head = get_output_service().load()
        modes = head.get("modes") if isinstance(head, Mapping) else None
        spec = modes.get(head.get("active_mode")) if isinstance(modes, Mapping) else None
        routing = spec.get("routing") if isinstance(spec, Mapping) else None
        if isinstance(routing, Mapping):
            for assignments in routing.values():
                if any(role not in ("off", "main_l", "main_r")
                       for role in (assignments or [])):
                    return OUTPUT_MODE_SUBWOOFER_22
    except Exception:
        pass
    return OUTPUT_MODE_STEREO


def _make_dsp_orchestration_deps() -> DspOrchestrationDeps:
    """Bind the DSP/output orchestration to the application's runtime services.

    All entries resolve the current runtime state at call time, so tests that
    patch main.runtime attributes observe the patched services.
    """
    return DspOrchestrationDeps(
        get_dsp_runtime=lambda: runtime.dsp_runtime,
        get_dsp_manager=lambda: dsp_manager,
        get_audio_output_overview=lambda: get_audio_output_overview(),
        get_samplerate_status=lambda: get_samplerate_status(),
        get_current_force_rate=lambda: samplerate.get_current_pipewire_force_rate(),
        get_measurement_sr_session=lambda: measurement_sr_session,
        get_player_instance=lambda: runtime.player_instance,
        get_current_track_info=lambda: playback_state.current_track_info,
        get_peak_monitor=lambda: runtime.peak_monitor,
        peak_monitor_playback_armed=lambda: peak_monitor_coordinator.armed,
        set_peak_monitor_context_signature=peak_monitor_coordinator.set_signature,
        get_spotify_ui_state=lambda *args, **kwargs: get_spotify_ui_state(*args, **kwargs),
        get_qobuz_ui_state=lambda *args, **kwargs: get_qobuz_ui_state(*args, **kwargs),
        sync_peak_monitor_for_playback_state=peak_monitor_coordinator.sync_playback_state,
        sync_peak_monitor_for_spotify_state=peak_monitor_coordinator.sync_spotify_state,
        sync_peak_monitor_for_qobuz_state=peak_monitor_coordinator.sync_qobuz_state,
        load_dsp_preset=lambda *args, **kwargs: _load_dsp_preset(*args, **kwargs),
        broadcast=lambda message: manager.broadcast(message),
        wait_for_samplerate_alignment=lambda *args, **kwargs: samplerate.wait_for_samplerate_alignment(*args, **kwargs),
        wait_for_selected_output_effective_rate=lambda *args, **kwargs: _wait_for_selected_output_effective_rate(*args, **kwargs),
        measurement_audio_graph_owned=lambda: playback_orchestration.configured().measurement_audio_graph_owned(),
        observe_playback_samplerate_drift=lambda: samplerate_drift.observe(),
        playback_transition_is_active=lambda: _playback_transition_is_active(),
        coordinator_target_rate=lambda *args, **kwargs: _coordinator_target_rate(*args, **kwargs),
        playback_graph_diagnosis=lambda *args, **kwargs: playback_orchestration.configured().playback_graph_diagnosis(*args, **kwargs),
        request_coordinated_recovery=lambda *args, **kwargs: playback_orchestration.configured().request_coordinated_recovery(*args, **kwargs),
        create_lifecycle_background_task=lambda coro, *, name: _create_lifecycle_background_task(coro, name=name),
        peak_monitor_restart_settle_ms=PEAK_MONITOR_RESTART_SETTLE_MS,
        sleep=lambda delay: asyncio.sleep(delay),
        get_output_mode=lambda: _current_output_mode(),
        reconcile_output_default=lambda: samplerate.reconcile_selected_output_default(),
        read_output_default_state=lambda: samplerate.selected_output_default_state(),
        get_coordinator_lock=lambda: getattr(playback_transition_coordinator, "lock", None),
        # Only the deliberate stale-helper repair retargets the pin, and it does
        # so through the canonical bounded reconcile policy (same path playback
        # uses) instead of a bare pw-metadata write.
        ensure_playback_samplerate_force=lambda *a, measurement_blocks_rate=_measurement_blocks_playback_rate, **k: samplerate.ensure_playback_samplerate_force(
            *a, measurement_blocks_rate=measurement_blocks_rate, **k
        ),
        # Idle-graph renegotiation trigger for the stale-helper sink nudge: a
        # fully idle sink ignores force-rate writes and suspend/resume pulses.
        trigger_idle_sink_renegotiation=lambda *a, **k: samplerate.trigger_idle_sink_renegotiation(*a, **k),
        try_render_v2_target=lambda rate, overview: _try_render_v2_sync_target(rate, overview),
    )


def _make_playback_orchestration_deps() -> playback_orchestration.PlaybackOrchestrationDeps:
    """Bind transition/recovery orchestration to live application services."""
    return playback_orchestration.PlaybackOrchestrationDeps(
        get_coordinator=lambda: playback_transition_coordinator,
        set_coordinator=lambda value: globals().__setitem__("playback_transition_coordinator", value),
        make_transition_coordinator=lambda: PlaybackTransitionCoordinator(
            FxrouteTransitionRuntime(make_playback_runtime_deps()),
            gate_state_path=_playback_gate_state_path(),
            get_source_generation=lambda: playback_state.source_generation,
        ),
        begin_transition_attempt=_begin_playback_transition_attempt,
        end_transition_attempt=_end_playback_transition_attempt,
        run_transition=lambda request: _run_coordinated_transition(request),
        get_playback_state=lambda: playback_state,
        get_runtime_player=lambda: runtime.player_instance,
        get_dsp_runtime=lambda: runtime.dsp_runtime,
        get_dsp_manager=lambda: dsp_manager,
        get_dsp_preset_load_lock=lambda: runtime.dsp_preset_load_lock,
        get_measurement_session=lambda: measurement_sr_session,
        get_samplerate_status=lambda: get_samplerate_status(),
        get_audio_output_overview=lambda *args, **kwargs: get_audio_output_overview(*args, **kwargs),
        get_spotify_ui_state=lambda *args, **kwargs: get_spotify_ui_state(*args, **kwargs),
        get_player_audio_samplerate=lambda: media_readiness.get_player_audio_samplerate(runtime.player_instance),
        is_local_playback_active=_is_local_playback_active,
        is_spotify_playback_active=_is_spotify_playback_active,
        spotify_target_track=_spotify_target_track_from_state,
        sample_rate_policy_is_auto=lambda: samplerate.load_sample_rate_policy().get("mode") == "auto",
        get_player_queue_fields=lambda: playback_queue.queue.native_request_fields(),
        run_pw_link_command=lambda *args: pw_link.run_pw_link_command(*args),
        connect_ports=lambda *args: pw_link.connect_ports(*args),
        contains_link=_contains_link,
        helper_argument_sample_rate=dsp_orchestration.helper_argument_sample_rate,
        sync_preset_for_samplerate=lambda *args, **kwargs: dsp_orchestrator.sync_preset_for_playback_samplerate(*args, **kwargs),
        sync_runtime=lambda *args, **kwargs: dsp_orchestrator.sync_runtime(*args, **kwargs),
        reconcile_sink_rate=lambda *args, measurement_blocks_rate=_measurement_blocks_playback_rate, **kwargs: samplerate.reconcile_transition_sink_rate(
            *args, measurement_blocks_rate=measurement_blocks_rate, **kwargs
        ),
        load_dsp_preset=lambda *args, **kwargs: _load_dsp_preset(*args, **kwargs),
        sleep=lambda delay: asyncio.sleep(delay),
        pipewire_poll_interval_ms=media_readiness.PIPEWIRE_HANDOFF_POLL_INTERVAL_MS,
        dsp_port_timeout_ms=PLAYBACK_HANDOFF_DSP_PORT_TIMEOUT_MS,
        post_start_readbacks=POST_START_GRAPH_STABILITY_READBACKS,
        output_mode_subwoofer_modes=frozenset(OUTPUT_MODE_SUBWOOFER_MODES),
        output_mode_stereo=OUTPUT_MODE_STEREO,
        get_dsp_snapshot=lambda: runtime.dsp_runtime.snapshot() if runtime.dsp_runtime is not None else {},
        mpv_source_ports_present=lambda: media_readiness.mpv_source_ports_present(),
        mpv_link_repair_timeout_ms=MPV_LINK_REPAIR_TIMEOUT_MS,
        source_port_readiness_timeout_ms=media_readiness.RADIO_SOURCE_PORT_READINESS_TIMEOUT_MS,
        # Let the extracted owner use the supplied low-level PipeWire
        # primitives; do not route this dependency through its public wrapper.
        repair_stereo_output_links=None,
        resolve_source_producer_ports=lambda source: _resolve_playback_source_producer_ports(source),
        list_spotify_sink_inputs=lambda: media_readiness.list_spotify_sink_inputs(),
        sync_plan_runtime=lambda *a, **k: _sync_plan_runtime(*a, **k),
        get_source_generation=lambda: playback_state.source_generation,
    )


playback_orchestration.configure(_make_playback_orchestration_deps())

# Bound orchestration entry points retained for legacy internal callers in
# main.py; implementations live in playback/orchestration.py.  Subsystem wiring
# reaches the orchestrator directly via playback_orchestration.configured().
_coordinator_target_rate = playback_orchestration.configured().coordinator_target_rate
_sample_rate_policy_is_auto = playback_orchestration.configured().sample_rate_policy_is_auto
_transition_sample_rate_policy = playback_orchestration.configured().transition_sample_rate_policy
_coordinator_current_playback_context = playback_orchestration.configured().current_playback_context
_coordinator_rate_change = playback_orchestration.configured().coordinator_rate_change
_playback_transition_is_active = playback_orchestration.configured().transition_is_active
_run_coordinated_transition = playback_orchestration.configured().run_coordinated_transition
_playback_transition_context_is_current = playback_orchestration.configured().playback_transition_context_is_current


# Response compression for HTML/CSS/JS/JSON. The path filter is the outermost
# middleware (first entry in the stack) so it hides gzip support from requests
# for already-compressed asset formats before GZipMiddleware decides;
# on-the-fly compression there would burn CPU for negligible size gain.
_GZIP_SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico",
    ".woff", ".woff2",
    ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav",
    ".mp4", ".webm", ".zip", ".gz", ".br", ".zst",
)


class _SkipPrecompressedAssetsForGZip:
    """Strips gzip from Accept-Encoding for pre-compressed asset paths."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].lower().endswith(_GZIP_SKIP_SUFFIXES):
            headers = MutableHeaders(scope=scope)
            accept = headers.get("Accept-Encoding", "")
            if "gzip" in accept.lower():
                kept = ",".join(
                    part.strip() for part in accept.split(",")
                    if part.strip() and "gzip" not in part.lower()
                )
                headers["Accept-Encoding"] = kept
        await self.app(scope, receive, send)


app = FastAPI(
    lifespan=lifespan,
    middleware=[
        Middleware(TrustedOriginMiddleware),
        Middleware(_SkipPrecompressedAssetsForGZip),
        Middleware(GZipMiddleware, minimum_size=1024),
    ],
)
app.include_router(radio_api_router)
app.include_router(spl_calibration.router)
app.include_router(speaker_api.router)
app.include_router(library_api_router)
app.include_router(autosub.router)
app.include_router(measurement_session.router)

# Static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    html = (STATIC_DIR / "index.html").read_text()
    if effective_request_scheme(request) != "https":
        html = re.sub(r'\s*<link rel="manifest" href="/static/site\.webmanifest\?v=[^"]+">\n?', '', html, count=1)
    # The shell carries the versioned asset URLs: it must never be served
    # from the browser cache, or clients keep booting stale JS/CSS after a
    # deploy even though the assets themselves are cache-busted.
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store, must-revalidate"})

@app.get("/favicon.ico")
async def favicon_root():
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")

@app.get("/apple-touch-icon.png")
async def apple_touch_icon_root():
    return FileResponse(STATIC_DIR / "apple-touch-icon.png", media_type="image/png")

@app.get("/site.webmanifest")
async def site_webmanifest_root():
    return FileResponse(STATIC_DIR / "site.webmanifest", media_type="application/manifest+json")


def _tidal_provider():
    """Return the registered TIDAL provider."""
    return streaming.get_provider("tidal")


async def _resolve_tidal_track(track_id: str) -> dict:
    """Resolve TIDAL metadata + a playable stream URL into a track dict.

    The stream URL is resolved as late as possible here (the play boundary)
    and is never persisted back into the catalog/queue metadata.
    """
    provider = _tidal_provider()
    try:
        meta = await provider.get_track(track_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"TIDAL track unavailable: {exc}") from exc
    try:
        stream = await provider.resolve_stream(track_id)
    except HTTPException:
        raise
    except (tidal_auth.TidalAuthError, tidal_playback.TidalStreamError) as exc:
        raise streaming_api._tidal_http_error(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"TIDAL track unavailable: {exc}") from exc
    return {
        "id": str(meta.get("id") or track_id),
        "title": meta.get("title") or "",
        "artist": meta.get("artist") or "",
        "album": meta.get("album") or "",
        "art_url": meta.get("art_url") or "",
        "duration": float(meta.get("duration") or 0),
        "source": "tidal",
        "url": stream.get("url") or "",
        "sample_rate_hz": stream.get("sample_rate"),
        "bit_depth": stream.get("bit_depth"),
        "audio_format": stream.get("audio_format"),
    }


async def _resolve_tidal_track_meta(track_id: str) -> dict:
    """Resolve TIDAL metadata only (no stream URL) for a queue entry."""
    provider = _tidal_provider()
    meta = await provider.get_track(track_id)
    return {
        "id": str(meta.get("id") or track_id),
        "title": meta.get("title") or "",
        "artist": meta.get("artist") or "",
        "album": meta.get("album") or "",
        "art_url": meta.get("art_url") or "",
        "duration": float(meta.get("duration") or 0),
        "source": "tidal",
        "url": "",
    }


async def _resolve_tidal_stream_url(track: dict) -> str | None:
    """Resolve a TIDAL queue entry's stream URL, updating its audio info in place.

    Called by the queue just before a transition so short-lived stream URLs
    are always freshly resolved at play time.
    """
    provider = _tidal_provider()
    track_id = str(track.get("id") or "")
    if not track_id:
        return None
    try:
        stream = await provider.resolve_stream(track_id)
    except Exception as exc:
        logger.warning("TIDAL stream resolution failed for %s: %s", track_id, exc)
        return None
    track["url"] = stream.get("url") or ""
    track["sample_rate_hz"] = stream.get("sample_rate")
    track["bit_depth"] = stream.get("bit_depth")
    track["audio_format"] = stream.get("audio_format")
    return track["url"] or None


def _native_mpv_direct_selection_ready(active_queue_ids: list) -> bool:
    """Gate for the native-MPV direct-selection fast path in /api/play.

    set_playlist_pos() is fire-and-forget and reports success even against
    an empty or stale MPV playlist (e.g. after another source stopped MPV
    during a handoff). Only skip the coordinated transition when MPV
    actually holds the mirrored native playlist: the committed owner is an
    MPV source, MPV has a file loaded, and its playlist length matches the
    active queue. Anything else falls through to the regular
    playback/coordinator path so the track is really loaded and the owner
    is taken over.
    """
    owner = playback_state.current_playback_owner
    if owner is not None and not source_policy.is_mpv_source(owner):
        return False
    player = runtime.player_instance
    if player is None or not getattr(player, "_running", False):
        return False
    state = getattr(player, "state", None) or {}
    if not state.get("current_file"):
        return False
    try:
        count = player.get_property("playlist-count")
    except Exception:
        return False
    return isinstance(count, int) and count > 0 and count == len(active_queue_ids)


@app.post("/api/play")
async def play_track(req: PlayRequest):
    source_intent = _capture_source_intent()
    if not runtime.player_instance or not runtime.player_instance._running:
        if not await _drain_worker(_ensure_player_running):
            raise HTTPException(status_code=503, detail="Player not available")
    if not _can_send_play_command():
        state = runtime.player_instance.state
        return {
            "status": "playing" if not state.get("paused") else "paused",
            "url": state.get("current_file") or "",
            "track": playback_state.current_track_info or playback_state.last_track_info or {},
            "playback": build_playback_payload(state),
        }

    source = str(req.source or "local")
    if not source_policy.is_mpv_source(source):
        raise HTTPException(status_code=400, detail=f"Unsupported playback source: {source}")

    active_queue_ids = [item.get("id") for item in playback_queue.queue.tracks]
    if (
        source == "local"
        and playback_queue.queue.mode == "native_mpv"
        and req.queue_track_ids
        and list(req.queue_track_ids) == active_queue_ids
        and req.track_id in active_queue_ids
        and _native_mpv_direct_selection_ready(active_queue_ids)
    ):
        target_index = active_queue_ids.index(req.track_id)
        was_paused = bool(runtime.player_instance.state.get("paused"))
        selection_epoch = playback_state.playback_transition_epoch
        if not await playback_queue.queue.load_track(target_index, transition_reason="direct queue selection"):
            raise HTTPException(status_code=409, detail="Native queue navigation failed")
        if was_paused:
            # Selecting a track from a paused native queue must start it, like
            # the app_replace/coordinator path (should_play=True).  MPV keeps
            # pause across a playlist-pos jump, so resume explicitly instead of
            # reporting "playing" over a still-paused transport.  The native
            # selection above has no await after its active-transition guard;
            # enter the Coordinator lock immediately so the resume worker and
            # its commit cannot overlap a newer transition.
            async with _manual_transport_guard(expected_epoch=selection_epoch):
                await _drain_worker(runtime.player_instance.set_pause, False)
                if playback_state.playback_transition_epoch != selection_epoch:
                    raise HTTPException(status_code=409, detail="A playback transition completed first")
                _ensure_source_intent_current(source_intent)
                _mark_player_state_authoritative(runtime.player_instance.state)
                _mark_playback_intent_changed()
        track_info = dict(playback_queue.queue.tracks[target_index])
        new_state = runtime.player_instance.state
        return {
            "status": "playing" if not new_state.get("paused") else "paused",
            "url": str(track_info.get("url") or ""),
            "track": track_info,
            "playback": build_playback_payload(new_state),
        }

    previous_state = dict(runtime.player_instance.state)
    if source == "radio":
        track_info = None
        for station in get_stations():
            if station.id == req.track_id:
                track_info = {
                    "id": f"radio_{station.id}",
                    "title": station.name,
                    "artist": "Radio",
                    "source": "radio",
                    "url": station.stream_url,
                    "sample_rate_hz": RADIO_EXPECTED_SAMPLE_RATE_HZ,
                }
                break
        if not track_info:
            raise HTTPException(status_code=404, detail="Radio station not found")
        queue_candidate = playback_queue.cleared_queue_candidate(track_info)
    elif source == "tidal":
        if not await _tidal_provider().is_authenticated():
            raise HTTPException(status_code=401, detail="TIDAL is not authenticated")
        track_info = await _resolve_tidal_track(req.track_id)
        queue_ids = [str(item) for item in (req.queue_track_ids or [])]
        if not queue_ids:
            queue_ids = [str(req.track_id)]
        elif str(req.track_id) not in queue_ids:
            queue_ids.insert(0, str(req.track_id))
        # Resolve remaining queue metadata concurrently; each entry is an
        # independent TIDAL API round trip and serial resolution dominated
        # album/playlist start latency.
        other_ids = [str(tidal_id) for tidal_id in queue_ids if str(tidal_id) != str(req.track_id)]

        async def _resolve_tidal_meta_safe(tidal_id: str) -> dict | None:
            try:
                return await _resolve_tidal_track_meta(tidal_id)
            except HTTPException as exc:
                logger.warning("TIDAL queue track %s skipped: %s", tidal_id, exc.detail)
                return None

        resolved_meta = (
            await asyncio.gather(*(_resolve_tidal_meta_safe(t) for t in other_ids))
            if other_ids
            else []
        )
        resolved_by_id = dict(zip(other_ids, resolved_meta))
        queue_tracks: list[dict] = []
        for tidal_id in queue_ids:
            key = str(tidal_id)
            if key == str(req.track_id):
                queue_tracks.append(track_info)
            else:
                entry = resolved_by_id.get(key)
                if entry is not None:
                    queue_tracks.append(entry)
        multi_track = len(queue_tracks) > 1
        # A new queue with shuffle on applies the established shuffle semantics
        # immediately (current entry fixed, everything else permuted).  The
        # original order is kept unshuffled so disabling shuffle restores it.
        original_queue_tracks = [dict(item) for item in queue_tracks]
        if multi_track and bool(req.shuffle):
            current_index = next(
                (index for index, item in enumerate(queue_tracks) if str(item.get("id")) == str(req.track_id)),
                0,
            )
            queue_tracks = playback_queue.shuffle_around_current(queue_tracks, current_index)
        track_index = next(
            (index for index, item in enumerate(queue_tracks) if str(item.get("id")) == str(req.track_id)),
            -1,
        )
        queue_candidate = playback_queue.QueueCandidate(
            queue=[dict(item) for item in queue_tracks] if multi_track else [],
            original=original_queue_tracks if multi_track else [],
            index=track_index if multi_track else -1,
            mode="app_replace",
            loop=bool(req.loop),
            shuffle=bool(req.shuffle) and multi_track,
            single_track_loop=bool(req.loop) and not multi_track,
            track=track_info,
        )
    else:
        preserve_queue_order = bool(req.queue_track_ids) and list(req.queue_track_ids) == active_queue_ids
        scanner = runtime.music_library.scanner
        tracks = await _drain_worker(scanner.get_tracks) if scanner is not None else None
        queue_candidate = playback_queue.queue.prepare_local_queue(
            req.track_id,
            req.queue_track_ids,
            shuffle=req.shuffle,
            loop=req.loop,
            reshuffle=not preserve_queue_order,
            tracks=tracks,
            active_shuffle=playback_queue.queue.shuffle,
        )
        track_info = queue_candidate.track
    if not track_info or not track_info.get("url"):
        raise HTTPException(status_code=404, detail="Track not found")

    target_url = str(track_info.get("url") or "")
    native_queue_fields = playback_queue.queue.native_request_fields(queue_candidate) if source == "local" else {}
    native_trim_required = playback_queue.queue.mode == "native_mpv" and queue_candidate.mode != "native_mpv"
    same_target = previous_state.get("current_file") == target_url and not previous_state.get("ended")
    target_rate = _coordinator_target_rate(source, track_info)
    rate_change = await asyncio.to_thread(_coordinator_rate_change, target_rate)
    request = TransitionRequest(
        operation="play",
        source=source,
        target_rate=target_rate,
        target_url=target_url,
        target_track=dict(track_info),
        should_play=True,
        rate_change=rate_change,
        reload_source=bool(native_queue_fields)
        or (not same_target)
        or target_rate is None
        or rate_change,
        detail=f"title={track_info.get('title') or track_info.get('id')}",
        **native_queue_fields,
    )
    try:
        result = await _run_coordinated_transition(request)
    except ValueError as exc:
        raise bad_request(exc) from exc
    except PlaybackTransitionFailure as exc:
        # The committed queue state was never touched: the candidate is only
        # published after a successful commit below.  MPV's native playlist
        # also stays untouched on this path, so the committed native queue
        # remains fully navigable.
        raise _transition_error_http(exc) from exc
    if not getattr(result, "committed", False):
        # An uncommitted transition outcome is a failure and must not replace
        # the committed queue state either. A source-change discard maps to
        # 409 so the caller retries against the new source routing.
        _raise_for_uncommitted_transition(result, what="playback")
    _ensure_source_intent_current(source_intent)
    if native_trim_required:
        # The committed queue lived in MPV's native playlist.  The new target
        # is an app-side source (single track or mixed-rate): trim MPV's
        # playlist to the current file only after the transition committed,
        # so a non-reload transition cannot advance through stale
        # committed-queue entries and a failed play leaves the native
        # transport queue intact.
        try:
            playback_queue.queue.reduce_native_playlist_to_current()
            playback_queue.queue.reset_mpv_loop_state()
        except Exception:
            logger.warning(
                "Failed to trim native playlist after committed play transition",
                exc_info=True,
            )
    if source_policy.is_mpv_source(source) and isinstance(result.target_rate, int) and result.target_rate > 0:
        track_info["sample_rate_hz"] = result.target_rate

    playback_queue.queue.commit(queue_candidate)
    _commit_coordinated_track(
        track_info, source=source, commit_token=getattr(result, "transition_id", None)
    )
    return {
        "status": "playing",
        "url": target_url,
        "track": track_info,
        "playback": build_playback_payload(runtime.player_instance.state),
    }

@app.post("/api/pause")
async def pause_playback():
    if not runtime.player_instance or not runtime.player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if _playback_transition_is_active():
        raise HTTPException(status_code=409, detail="A playback transition is in progress")

    state = runtime.player_instance.state
    if not state.get("current_file") or state.get("ended"):
        raise HTTPException(status_code=409, detail="Nothing is currently loaded to pause or resume")
    # v0.9.4 contract: this endpoint is a pure MPV pause toggle.  It must not
    # rebuild the committed source/rate/graph just because transport changed.
    async with _manual_transport_guard():
        await _drain_worker(runtime.player_instance.pause)
        new_state = runtime.player_instance.state
        _mark_player_state_authoritative(new_state)
        _mark_playback_intent_changed()
    return {
        "status": "paused" if new_state.get("paused") else "playing",
        "playback": build_playback_payload(new_state),
    }


async def _spotify_global_control(action: str, request: Request | None = None) -> dict:
    """Global transport for a Spotify-owned playback context."""
    if action == "toggle":
        data = await get_spotify_ui_state()
        if data.get("status") == "Playing":
            data = await spotify_pause()
        else:
            data = await spotify_play()
        return await broadcast_spotify_state(data)
    if action == "next":
        return await broadcast_spotify_state(await spotify_next())
    if action == "previous":
        return await broadcast_spotify_state(await spotify_previous())
    if action == "seek":
        body = await _json_object(request) if request is not None else {}
        try:
            position = float(body.get("position", 0))
        except (ValueError, TypeError) as exc:
            raise bad_request(exc) from exc
        return await broadcast_spotify_state(await spotify_seek_to(position))
    if action == "shuffle":
        return await broadcast_spotify_state(await spotify_shuffle_toggle())
    if action == "loop":
        return await broadcast_spotify_state(await spotify_loop_cycle())
    return {}


async def _qobuz_global_control(action: str, request: Request | None = None) -> dict:
    """Global transport for a Qobuz-owned playback context."""
    provider = streaming.get_provider("qobuz")
    if action == "toggle":
        data = await get_qobuz_ui_state()
        if data.get("status") == "Playing":
            data = await qobuz_pause()
        else:
            data = await qobuz_play()
        return await broadcast_qobuz_state(data)
    if action == "next":
        return await broadcast_qobuz_state(await provider.next())
    if action == "previous":
        return await broadcast_qobuz_state(await provider.previous())
    if action == "seek":
        body = await _json_object(request) if request is not None else {}
        try:
            position = float(body.get("position", 0))
        except (ValueError, TypeError) as exc:
            raise bad_request(exc) from exc
        return await broadcast_qobuz_state(await provider.seek(position))
    if action == "shuffle":
        return await broadcast_qobuz_state(await provider.shuffle())
    if action == "loop":
        return await broadcast_qobuz_state(await provider.repeat())
    return {}


async def _route_global_control(action: str, request: Request | None = None) -> dict | None:
    """Route a global transport action to the authoritative owner's adapter.

    Returns None when the action must fall through to the native MPV path.
    """
    owner = _resolve_playback_owner()
    if owner == "spotify":
        return await _spotify_global_control(action, request)
    if owner == "qobuz":
        return await _qobuz_global_control(action, request)
    return None


@app.post("/api/playback/toggle")
async def toggle_playback():
    toggle_epoch = playback_state.playback_transition_epoch
    source_intent = _capture_source_intent()
    async with _manual_transport_guard(expected_epoch=toggle_epoch):
        routed = await _route_global_control("toggle")
    if routed is not None:
        return routed
    if not runtime.player_instance or not runtime.player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if _playback_transition_is_active():
        raise HTTPException(status_code=409, detail="A playback transition is in progress")
    if not _can_send_play_command():
        state = runtime.player_instance.state
        return {"status": "paused" if state.get("paused") else "playing", "playback": build_playback_payload(state)}

    state = dict(runtime.player_instance.state)
    active_track = dict(playback_state.current_track_info or {})
    if state.get("current_file") and not state.get("ended") and source_policy.is_mpv_source(active_track.get("source")):
        was_paused = bool(state.get("paused"))
        source = str(active_track.get("source"))
        if not was_paused:
            # Same-source pause is transport only.  Resuming below remains a
            # Coordinator transition because it is a Local/Radio play action.
            async with _manual_transport_guard(expected_epoch=toggle_epoch):
                await _drain_worker(runtime.player_instance.pause)
                new_state = runtime.player_instance.state
                _mark_player_state_authoritative(new_state)
                _mark_playback_intent_changed()
            return {
                "status": "playing" if not new_state.get("paused") else "paused",
                "playback": build_playback_payload(new_state),
            }
        target_rate = _coordinator_target_rate(source, active_track)
        rate_change = await asyncio.to_thread(_coordinator_rate_change, target_rate)
        if not rate_change and target_rate is not None:
            # Same-rate resume is transport-only: the committed source, rate
            # and graph are unchanged while paused, so a full Coordinator
            # transition (gate close, quiet, effects/graph re-verification) is
            # pure latency and makes the footer flash.  Unpause directly,
            # symmetric to the pause fast path above.  The pre-await epoch
            # rejects a context that a transition committed in the meantime.
            async with _manual_transport_guard(expected_epoch=toggle_epoch):
                await _drain_worker(runtime.player_instance.set_pause, False)
                new_state = runtime.player_instance.state
                _ensure_source_intent_current(source_intent)
                _mark_player_state_authoritative(new_state)
                _mark_playback_intent_changed()
            return {
                "status": "playing" if not new_state.get("paused") else "paused",
                "playback": build_playback_payload(new_state),
            }
        request = TransitionRequest(
            operation="resume",
            source=source,
            target_rate=target_rate,
            target_url=str(active_track.get("url") or state.get("current_file") or ""),
            target_track=active_track,
            should_play=True,
            rate_change=rate_change,
            reload_source=(target_rate is None or rate_change),
            detail="toggle-resume",
            **((playback_queue.queue.native_request_fields()) if source == "local" else {}),
        )
        try:
            result = await _run_coordinated_transition(request)
        except ValueError as exc:
            raise bad_request(exc) from exc
        except PlaybackTransitionFailure as exc:
            raise _transition_error_http(exc) from exc
        if not getattr(result, "committed", False):
            _raise_for_uncommitted_transition(result, what="playback")
        _ensure_source_intent_current(source_intent)
        if was_paused:
            if _sample_rate_policy_is_auto() and source_policy.is_mpv_source(source) and isinstance(result.target_rate, int) and result.target_rate > 0:
                active_track["sample_rate_hz"] = result.target_rate
            _commit_coordinated_track(
                active_track, source=source, commit_token=getattr(result, "transition_id", None)
            )
        new_state = runtime.player_instance.state
        return {
            "status": "playing" if not new_state.get("paused") else "paused",
            "playback": build_playback_payload(new_state),
        }

    replay_track = dict(playback_state.current_track_info or playback_state.last_track_info or {})
    replay_url = str(replay_track.get("url") or "")
    if not replay_url:
        raise HTTPException(status_code=409, detail="Nothing is available to replay")
    source = str(replay_track.get("source") or "local")
    target_rate = _coordinator_target_rate(source, replay_track)
    replay_rate_change = await asyncio.to_thread(_coordinator_rate_change, target_rate)
    request = TransitionRequest(
        operation="replay",
        source=source,
        target_rate=target_rate,
        target_url=replay_url,
        target_track=replay_track,
        should_play=True,
        rate_change=replay_rate_change,
        reload_source=True,
        detail="replay",
        **((playback_queue.queue.native_request_fields()) if source == "local" else {}),
    )
    try:
        result = await _run_coordinated_transition(request)
    except ValueError as exc:
        raise bad_request(exc) from exc
    except PlaybackTransitionFailure as exc:
        raise _transition_error_http(exc) from exc
    if not getattr(result, "committed", False):
        _raise_for_uncommitted_transition(result, what="playback")
    _ensure_source_intent_current(source_intent)
    if _sample_rate_policy_is_auto() and source_policy.is_mpv_source(source) and isinstance(result.target_rate, int) and result.target_rate > 0:
        replay_track["sample_rate_hz"] = result.target_rate
    _commit_coordinated_track(
        replay_track, source=source, commit_token=getattr(result, "transition_id", None)
    )
    return {
        "status": "playing",
        "replayed": True,
        "playback": build_playback_payload(runtime.player_instance.state),
    }

@app.post("/api/stop")
async def stop_playback():
    if not runtime.player_instance or not runtime.player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if _playback_transition_is_active():
        raise HTTPException(status_code=409, detail="A playback transition is in progress")
    async with _manual_transport_guard():
        owner = playback_state.current_playback_owner
        _mark_playback_intent_changed()
        if owner == "spotify":
            # Update the cached provider state before clearing the owner so
            # the read-only owner derivation cannot immediately re-derive
            # spotify from stale Playing telemetry, then wait bounded for the
            # renderer's sink input to actually disappear.
            data = await spotify_pause()
            playback_state.latest_spotify_state = data
            try:
                await media_readiness.wait_for_pipewire_spotify_release()
            except Exception as exc:
                logger.debug("Spotify sink release wait skipped: %s", exc)
        elif owner == "qobuz":
            await qobuz_pause()
            try:
                await media_readiness.wait_for_pipewire_qobuz_release()
            except Exception as exc:
                logger.debug("Qobuz sink release wait skipped: %s", exc)
        if playback_state.current_track_info and playback_state.current_track_info.get("source") == "radio":
            playback_state.last_radio_track_info = dict(playback_state.current_track_info)
        playback_state.current_track_info = None
        playback_state.current_playback_owner = None
        radio_reconnect.reset()
        playback_queue.queue.reset()
        playback_queue.queue.reset_mpv_loop_state()
        await _drain_worker(runtime.player_instance.stop_playback)
        _mark_player_state_authoritative(runtime.player_instance.state)
        # Playback is idle: a force-rate pin left by the last source rate is stale
        # under an auto policy and would keep the live samplerate payload pinned to
        # that rate (and previously misreported mode=fixed).  Clear it so the
        # graph is unpinned and the payload reflects the auto policy.  Fixed
        # policies keep their pin (they intentionally hold the configured rate).
        # An open measurement window owns the live rate (its sweep runs at the
        # measurement rate): clearing the pin there unpins the graph mid-window
        # and makes the session release read its own pin as an external change,
        # so the playback rate is never restored.  The release clears it.
        if _measurement_session_owns_live_rate():
            logger.info(
                "Stop skipped the auto-policy force-rate clear: "
                "measurement session owns the audio graph"
            )
        else:
            try:
                status = await asyncio.to_thread(get_samplerate_status)
            except Exception:
                status = None
            samplerate.clear_auto_policy_force_rate(
                int((status or {}).get("active_rate") or 0),
                status=status,
                idle=True,
            )
    return {"status": "stopped"}

@app.post("/api/volume")
async def set_volume(request: Request):
    if not runtime.player_instance or not runtime.player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    try:
        body = await request.json()
        vol = int(body.get("volume", 50))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body, expected {\"volume\": <int>}")
    try:
        volume_result = await _set_canonical_output_volume(vol)
    except SystemVolumeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to set output volume: {exc}")
    await _drain_worker(ensure_local_source_volume)
    await manager.broadcast({"type": "playback", "data": build_playback_payload(runtime.player_instance.state)})
    return {"status": "ok", "volume": volume_result["volume"]}

@app.post("/api/playback/next")
async def next_playback():
    routed = await _route_global_control("next")
    if routed is not None:
        return routed
    if not runtime.player_instance or not runtime.player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if len(playback_queue.queue.tracks) <= 1:
        raise HTTPException(status_code=409, detail="No queue is active")
    result = await playback_queue.queue.advance(transition_reason="manual queue next")
    if result == "unavailable":
        raise HTTPException(status_code=409, detail="Already at the end of the queue")
    if result == "ended":
        # The terminal queue end state was committed (queue cleared, index
        # reset).  Building the payload after the clear exposes the cleared
        # queue to the client instead of reporting a failure for a state
        # that was already changed.
        return {
            "status": "ok",
            "advanced": False,
            "queue_ended": True,
            "playback": build_playback_payload(runtime.player_instance.state),
        }
    return {"status": "playing", "playback": build_playback_payload(runtime.player_instance.state)}


@app.post("/api/playback/previous")
async def previous_playback():
    routed = await _route_global_control("previous")
    if routed is not None:
        return routed
    if not runtime.player_instance or not runtime.player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if len(playback_queue.queue.tracks) <= 1:
        raise HTTPException(status_code=409, detail="No queue is active")
    if not await playback_queue.queue.rewind(transition_reason="manual queue previous"):
        raise HTTPException(status_code=409, detail="Already at the start of the queue")
    return {"status": "playing", "playback": build_playback_payload(runtime.player_instance.state)}


@app.post("/api/playback/clear-queue")
async def clear_playback_queue():
    if not runtime.player_instance or not runtime.player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if _playback_transition_is_active():
        raise HTTPException(status_code=409, detail="A playback transition is in progress")

    had_queue = len(playback_queue.queue.tracks) > 1
    playback_queue.queue.reset()
    playback = build_playback_payload(runtime.player_instance.state)
    await manager.broadcast({"type": "playback", "data": playback})
    return {"status": "cleared" if had_queue else "idle", "playback": playback}


@app.post("/api/playback/shuffle")
async def set_playback_shuffle(request: Request):
    body = await _json_object(request, detail='Invalid JSON, expected {"enabled": <bool>}')
    if not isinstance(body.get("enabled"), bool):
        # No explicit target state: legacy global behavior, route to the
        # current playback owner (e.g. Spotify/Qobuz toggle).
        routed = await _route_global_control("shuffle", request)
        if routed is not None:
            return routed
        raise HTTPException(status_code=400, detail="Invalid JSON, expected {\"enabled\": <bool>}")
    # An explicit enabled value is always a library intent: set the local
    # queue shuffle directly, regardless of which provider currently owns
    # playback. Routing it to the owner would adopt the toggle on the wrong
    # backend (ignoring `enabled`) while the UI keeps showing the local
    # queue state.
    enabled = bool(body["enabled"])
    if not runtime.player_instance or not runtime.player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")

    try:
        if not await playback_queue.queue.set_shuffle(enabled):
            raise HTTPException(status_code=409, detail="Shuffle requires an active local queue")
    except PlaybackTransitionFailure as exc:
        raise _transition_error_http(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"message": "Native queue reorder failed", "error": str(exc)},
        ) from exc

    playback = build_playback_payload(runtime.player_instance.state)
    await manager.broadcast({"type": "playback", "data": playback})
    return {"status": "ok", "shuffle": playback["queue"].get("shuffle", False), "playback": playback}


@app.post("/api/playback/loop")
async def set_playback_loop(request: Request):
    routed = await _route_global_control("loop", request)
    if routed is not None:
        return routed
    if not runtime.player_instance or not runtime.player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    try:
        body = await request.json()
        enabled = bool(body.get("enabled", False))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON, expected {\"enabled\": <bool>}")

    if not playback_queue.queue.set_loop(enabled):
        raise HTTPException(status_code=409, detail="Loop requires active local playback")

    playback = build_playback_payload(runtime.player_instance.state)
    await manager.broadcast({"type": "playback", "data": playback})
    return {"status": "ok", "loop": playback["queue"].get("loop", False), "playback": playback}


@app.post("/api/playback/seek")
async def seek_playback(request: Request):
    seek_epoch = playback_state.playback_transition_epoch
    async with _manual_transport_guard(expected_epoch=seek_epoch):
        routed = await _route_global_control("seek", request)
    if routed is not None:
        return routed
    if not runtime.player_instance or not runtime.player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if _playback_transition_is_active():
        raise HTTPException(status_code=409, detail="A playback transition is in progress")
    try:
        body = await _json_object(request)
        pos = float(body.get("position", 0))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON, expected {\"position\": <float>}")
    if not runtime.player_instance.state.get("current_file"):
        raise HTTPException(status_code=409, detail="Nothing loaded to seek")
    # Re-check after the last await: a transition may have started while the
    # request body was being read.  Never seek or mark intent mid-transition.
    if _playback_transition_is_active():
        raise HTTPException(status_code=409, detail="A playback transition is in progress")
    if not _can_send_play_command():
        state = runtime.player_instance.state
        return {"status": "ok", "position": state.get("position", 0), "playback": build_playback_payload(state)}
    async with _manual_transport_guard(expected_epoch=seek_epoch):
        await _drain_worker(runtime.player_instance.seek, pos)
        _mark_playback_intent_changed()
    return {"status": "ok", "position": pos, "playback": build_playback_payload(runtime.player_instance.state)}

@app.get("/api/status")
async def get_status():
    if runtime.player_instance:
        state = build_playback_payload(runtime.player_instance.state, include_live_metadata=False)
        state["metadata"] = (
            await _read_status_player_detail(runtime.player_instance.get_metadata, {})
            if state.get("current_file") else {}
        )
        if track := (state.get("current_track") or {}):
            if track.get("source") == "radio":
                state["live_title"] = (
                    state["metadata"].get("icy-title")
                    or state["metadata"].get("title")
                    or None
                )
        track = state.get("current_track") or {}
        if track.get("source") == "radio":
            station_id = str(track.get("id") or "").removeprefix("radio_")
            stream_url = str(track.get("url") or "")
            # Provider lookup is metadata-only and occurs after the playback
            # payload has been built.  It cannot affect loadfile or audio state.
            provider_metadata = await radio_metadata_service.get(station_id, stream_url)
            active_track = (playback_state.current_track_info or {})
            active_station_id = str(active_track.get("id") or "").removeprefix("radio_")
            if provider_metadata and station_id == active_station_id:
                state["radio_metadata"] = provider_metadata
            else:
                icy_title = str(state.get("live_title") or "").strip()
                state["radio_metadata"] = {
                    "station_id": station_id,
                    "provider": None,
                    "track_id": f"icy:{station_id}:{icy_title}" if icy_title else None,
                    "artist": None,
                    "title": icy_title or None,
                    "album": None,
                    "cover_url": None,
                    "started_at": None,
                    "ends_at": None,
                    "duration_seconds": None,
                    "progress_seconds": None,
                    "history": [],
                    "source": "icy" if icy_title else "station",
                    "fetched_at": time.time(),
                    "stale": False,
                }
        else:
            state["radio_metadata"] = None
        # Live stream facts from mpv (codec/bitrate/samplerate/depth) for the
        # tech line.  Read-only; never derived from URLs or catalog fields.
        # A transient read gap (empty current_file, bounded property read
        # failure) must not degrade a complete track's facts to nothing, so
        # the ledger keeps the last known facts while the track identity is
        # stable — the footer meta-tag stays complete across rate/status
        # refreshes instead of collapsing to the bare rate.
        if track.get("source") in ("radio", "local", "tidal"):
            raw = (
                await _read_status_player_detail(runtime.player_instance.get_stream_audio_info, {})
                if state.get("current_file")
                else None
            )
            state["stream_info"] = stream_info_ledger.resolve(track, raw)
        else:
            state["stream_info"] = None
            stream_info_ledger.reset()
        # The live flag is intentionally provided twice: once top-level and
        # once inside ``system``. Both shapes have existing consumers — the
        # frontend banner accepts either (app.js updateLiveBanner:
        # data.live === true || data.system.live === true) and demo/test
        # transports synthesize one or the other. Do NOT remove either key
        # without updating all of them; this duplication is contract, not
        # accidental redundancy.
        state["system"] = {"version": _read_version_file(), "live": is_live_mode()}
        state["live"] = state["system"]["live"]
        return state
    live = is_live_mode()
    # Same intentional duplication as above (see the comment there).
    return {"running": False, "system": {"version": _read_version_file(), "live": live}, "live": live}


@app.get("/api/power/state")
async def get_power_state():
    return _build_power_state_payload()


@app.post("/api/power/measurement-heartbeat")
async def measurement_window_heartbeat(request: Request):
    global last_measurement_window_seen_at
    body = await _json_object(request)
    if body.get("open") is False:
        last_measurement_window_seen_at = 0.0
        if measurement_sr_session is not None:
            await measurement_sr_session.request_close()
    else:
        last_measurement_window_seen_at = time.monotonic()
        if measurement_sr_session is not None:
            await measurement_sr_session.request_open()
    return {
        "status": "ok",
        "measurement_window_open": _is_measurement_window_open(),
    }


@app.get("/api/system/update")
async def system_update_status():
    result = await _run_update_operation(_UPDATE_CHECK_TIMEOUT_SECONDS, "--check")
    return {
        "ok": result["returncode"] == 0,
        "installed_version": _read_version_file(),
        **result,
    }


async def _system_update_or_restore(request: Request, *script_args: str) -> dict:
    """Shared body of the privileged update/restore POSTs.

    Applies the same trusted-origin defence as the provider admin, Qobuz
    auth, power and device-name endpoints: a foreign or "null"
    Origin/Referer is rejected with 403 before any update-script or
    restart side effect; headerless CLI/systemd calls stay allowed.
    ``script_args`` are forwarded to update_fxroute.sh (update:
    ``--defer-restart``, restore: ``--restore --defer-restart``).
    """
    if not is_request_origin_trusted(request):
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    restore = "--restore" in script_args
    service_name = _configured_service_name()
    update_applied = False

    def schedule_restart(result: dict) -> None:
        nonlocal update_applied
        update_applied = update_lifecycle.should_schedule_restart(
            returncode=result["returncode"], stdout=result.get("stdout", ""), restore=restore
        )
        if update_applied:
            # Reserve before creating the task so no maintenance request
            # slips into the gap. The done-callback owns the release: a
            # task cancelled before its first step never runs its
            # coroutine finally, so the callback is the only guaranteed
            # release path. finish is idempotent, double release is safe.
            update_lifecycle.reserve_deferred_restart()
            try:
                task = _create_lifecycle_background_task(
                    _restart_fxroute_service_after_response(service_name), name="service-restart"
                )
            except BaseException:
                update_lifecycle.finish_deferred_restart()
                raise
            task.add_done_callback(lambda _done: update_lifecycle.finish_deferred_restart())

    result = await _run_update_operation(
        _UPDATE_APPLY_TIMEOUT_SECONDS, *script_args, on_result=schedule_restart
    )
    ok = result["returncode"] == 0
    return {
        "ok": ok,
        "installed_version": _read_version_file(),
        "restart_scheduled": update_applied,
        "service_name": service_name,
        **result,
    }


@app.post("/api/system/update")
async def system_update(request: Request):
    """Apply the update; guarded like the other privileged POSTs."""
    return await _system_update_or_restore(request, "--defer-restart")


@app.post("/api/system/restore")
async def system_restore(request: Request):
    """Restore the checkout to origin/main and return to a clean public release.

    This is an explicit repair action, not a normal update. It saves local
    source changes as a patch file in backups/, then resets the working tree
    to origin/main and restarts the service.

    User data, music, config, and runtime cache files are not affected.
    """
    return await _system_update_or_restore(request, "--restore", "--defer-restart")


# INFO only on state change; unchanged readback polls and transition bursts stay silent.
_last_logged_samplerate_signature = None


@app.get("/api/audio/samplerate")
async def audio_samplerate_status():
    status = await asyncio.to_thread(get_samplerate_status)
    signature = (
        playback_state.current_playback_owner,
        status.get("active_rate"),
        (status.get("relevant_sink") or {}).get("state"),
    )
    global _last_logged_samplerate_signature
    if signature != _last_logged_samplerate_signature:
        _last_logged_samplerate_signature = signature
        logger.info(
            "audio_samplerate_status entry: playback_owner=%s active_rate=%s sink_state=%s",
            *signature,
        )
    # This endpoint is a pure readback.  Any corrective action must enter the
    # PlaybackTransitionCoordinator through an explicit recovery request.
    return status


@app.post("/api/audio/samplerate")
async def save_audio_samplerate_policy(request: Request):
    if measurement_sr_session is not None and measurement_sr_session.has_active_jobs:
        raise HTTPException(status_code=423, detail="Measurement is active; sample-rate policy is locked")
    try:
        body = await request.json()
        policy = normalize_sample_rate_policy(body.get("mode"), body.get("rate"))
    except ValueError as exc:
        raise bad_request(exc) from exc
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid JSON body, expected {"mode": "auto"|"fixed", "rate": <number?>}')

    try:
        await _transition_sample_rate_policy(policy, detail="api-audio-samplerate-policy")
        return get_samplerate_status()
    except ValueError as exc:
        raise bad_request(exc) from exc
    except PlaybackTransitionFailure as exc:
        raise _transition_error_http(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save sample-rate policy: {exc}") from exc


@app.get("/api/hardware/status")
async def hardware_status():
    if hardware_controller is None:
        return {"available": False, "connected": False, "status": {}, "notes": ["hardware controller not initialized"]}
    return await asyncio.to_thread(hardware_controller.get_status)


async def _run_hardware_command(command: str):
    if hardware_controller is None:
        return {"available": False, "connected": False, "status": {}, "notes": ["hardware controller not initialized"]}
    return await asyncio.to_thread(hardware_controller.command, command)


@app.post("/api/hardware/input/rca")
async def hardware_input_rca():
    return await _run_hardware_command("SET INPUT RCA")


@app.post("/api/hardware/input/xlr")
async def hardware_input_xlr():
    return await _run_hardware_command("SET INPUT XLR")


@app.post("/api/hardware/input/press")
async def hardware_input_press():
    return await _run_hardware_command("PRESS INPUT")


@app.post("/api/hardware/auto/on")
async def hardware_auto_on():
    return await _run_hardware_command("AUTO ON")


@app.post("/api/hardware/auto/off")
async def hardware_auto_off():
    return await _run_hardware_command("AUTO OFF")


@app.get("/api/audio/outputs")
async def audio_output_overview():
    overview = with_subwoofer_derived_delays(await asyncio.to_thread(get_audio_output_overview))
    if runtime.dsp_runtime is not None:
        overview["output_mode"] = {
            **(overview.get("output_mode") or {}),
            "runtime": runtime.dsp_runtime.snapshot(),
        }
    return overview


@app.post("/api/audio/outputs")
async def save_audio_output_selection_route(request: Request):
    try:
        body = await request.json()
        output_key = str(body.get("key", "")).strip()
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid JSON body, expected {"key": <string>}')

    try:
        coordinator = playback_transition_coordinator
        if coordinator is None:
            raise RuntimeError("Playback transition coordinator is unavailable")
        async with coordinator.lock:
            async with measurement_sr_session.lock:
                if measurement_sr_session.has_active_jobs:
                    raise HTTPException(status_code=423, detail="Measurement is active; output selection is locked")
                result = await _shield_coro(_apply_audio_output_selection(output_key))
        await dsp_orchestrator.refresh_peak_monitor_after_effects_change("audio-output-switch")
        return result
    except HTTPException:
        raise
    except ValueError as exc:
        raise bad_request(exc)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to switch audio output: {exc}")


async def _verify_audio_output_selection(key: str, target: dict) -> None:
    snapshot = runtime.dsp_runtime.snapshot() if runtime.dsp_runtime is not None else {}
    if not snapshot.get("active") or (snapshot.get("config") or {}).get("output_key") != key:
        raise RuntimeError(f"Native DSP graph did not commit output {key}")
    diagnosis = await playback_orchestration.configured().playback_graph_diagnosis(target)
    if diagnosis.get("output_key") != key or not diagnosis.get("links_complete"):
        raise RuntimeError(f"Native DSP links did not verify for output {key}")


async def _apply_audio_output_selection(key: str) -> dict:
    """Apply and verify a target under Coordinator and rate ownership."""
    selected = await asyncio.to_thread(samplerate.prepare_audio_output_selection, key)
    previous = await asyncio.to_thread(get_audio_output_overview)
    previous_default = (previous.get("default_output") or {}).get("key")
    if not previous_default:
        raise RuntimeError("Previous default sink is unavailable")
    selection_path = samplerate._audio_output_selection_path()
    previous_bytes = await asyncio.to_thread(lambda: selection_path.read_bytes() if selection_path.exists() else None)
    previous_graph_key = ((runtime.dsp_runtime.snapshot().get("config") or {}).get("output_key")
                          if runtime.dsp_runtime is not None else None)
    target = await asyncio.to_thread(get_audio_output_overview, selection_key=key)
    if (target.get("selected_output") or {}).get("key") != key:
        raise RuntimeError("Selected output is no longer available")

    try:
        await asyncio.to_thread(samplerate._set_default_sink, selected["name"])
        await dsp_orchestrator.sync_runtime(
            reason="output-selection", target_overview=target, _rate_lock_held=True,
        )
        await _verify_audio_output_selection(key, target)
        # Re-read the live default before publishing the persistent selection.
        live = await asyncio.to_thread(get_audio_output_overview, selection_key=key)
        if (live.get("default_output") or {}).get("key") != selected["name"]:
            raise RuntimeError("Default sink changed before output selection commit")
        await asyncio.to_thread(samplerate._save_audio_output_selection, key)
    except BaseException:
        try:
            current_bytes = await asyncio.to_thread(
                lambda: selection_path.read_bytes() if selection_path.exists() else None
            )
            if current_bytes != previous_bytes:
                if previous_bytes is None:
                    await asyncio.to_thread(selection_path.unlink, missing_ok=True)
                else:
                    await asyncio.to_thread(atomic_write_bytes, selection_path, previous_bytes)
            await asyncio.to_thread(samplerate._set_default_sink, previous_default)
            if previous_graph_key and previous_graph_key != key:
                restore = await asyncio.to_thread(get_audio_output_overview, selection_key=previous_graph_key)
                try:
                    await _verify_audio_output_selection(previous_graph_key, restore)
                except RuntimeError:
                    await dsp_orchestrator.sync_runtime(
                        reason="output-selection-rollback", target_overview=restore, _rate_lock_held=True,
                    )
                await _verify_audio_output_selection(previous_graph_key, restore)
        except BaseException:
            logger.exception("Output selection rollback failed")
            raise RuntimeError("Output selection failed and previous graph could not be restored")
        raise

    result = with_subwoofer_derived_delays(await asyncio.to_thread(get_audio_output_overview))
    result["output_mode"] = {**(result.get("output_mode") or {}), "runtime": runtime.dsp_runtime.snapshot()}
    return result


_output_service_instance: OutputService | None = None


def _output_state_store_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_root / "fxroute" / "output-state.json"


def _resolve_state_ir(kernel):
    """Resolve a bank convolver kernel to its file and channel count."""
    manager = _require_dsp_manager()
    path = manager._resolve_kernel_path(kernel)
    params = parse_wav_frames(path)
    ensure_kernel_supported_ir(params, path.name)
    return {"path": str(path), "channels": params["channels"]}


def get_output_service() -> OutputService:
    """Return the authoritative output-state service (late-bound singleton)."""
    global _output_service_instance
    if _output_service_instance is None:
        _output_service_instance = OutputService(OutputServiceDeps(
            store=OutputStateStore(_output_state_store_path()),
            preset_loader=lambda name: _require_dsp_manager().preset_store.read(name),
            resolve_ir=_resolve_state_ir,
            measurement_active=lambda: measurement_sr_session is not None and bool(
                measurement_sr_session.has_active_jobs),
        ))
    return _output_service_instance


def _v2_head_for_overview() -> dict | None:
    """Total v2 head for the derived overview mode payload (None if unusable)."""
    try:
        return get_output_service().load()
    except Exception:
        return None


samplerate.configure_output_state_head(_v2_head_for_overview)


def _output_state_device(overview: dict) -> tuple[str, int | None]:
    mode = overview.get("output_mode") or {}
    selected = overview.get("selected_output") or {}
    key = str(mode.get("effective_output_key") or selected.get("key") or "")
    channels = mode.get("effective_output_channels", selected.get("channels"))
    if not key:
        raise HTTPException(status_code=400, detail="No audio output device is selected")
    if channels is None:
        return key, None
    try:
        count = int(channels)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Output channel capacity is unknown")
    if count < 0:
        raise HTTPException(status_code=400, detail="Output channel capacity is unknown")
    return key, count


def _freeze_measurement_target(bank_id: str, sample_rate_hz: int) -> dict:
    """Freeze the measurement target for the currently selected output device.

    Uses the committed output state and the same processing fingerprint the
    transition coordinator verifies, so a stored result can never claim
    processing the sweep did not run through.  An empty bank id follows the
    current editing selection (the area selector's Global default).
    """
    service = get_output_service()
    state = service.ensure_state()
    overview = get_audio_output_overview()
    output_key, channels = _output_state_device(overview)
    channel_count = int(channels or 0)
    topology = _output_state_topology(state, state["active_mode"], output_key, channel_count)
    raw_bank = str(bank_id or "").strip() or selected_bank(state["modes"][state["active_mode"]], topology["roles"])
    # A single actively routed way keeps its own target so per-way captures
    # isolate exactly that way; area ids still resolve to their owning bank.
    if raw_bank in topology["roles"]:
        bank = raw_bank
    else:
        bank = resolve_bank(state["modes"][state["active_mode"]], raw_bank, topology["roles"])["id"]
    fingerprint = service.fingerprint(state, output_key=output_key, channels=channel_count,
                                      sample_rate_hz=sample_rate_hz)
    return freeze_measurement_target(
        state, bank_id=bank, output_key=output_key, channels=channel_count,
        sample_rate_hz=sample_rate_hz, fingerprint=fingerprint)


def _live_measurement_sample_rate() -> int:
    """Sample rate the next measurement would run at (48 kHz fallback)."""
    store = measurement_store
    if store is None:
        return 48_000
    try:
        rate = int(store._resolve_measurement_sample_rate())
    except Exception as exc:
        logger.warning("Live measurement sample rate unavailable, using 48000 Hz: %s", exc)
        return 48_000
    return rate if rate > 0 else 48_000


def _verify_measurement_commit(measurement_id: str, binding: dict) -> None:
    """Gate a generated PEQ/FIR commit on its source measurement still fitting.

    The stored target must still describe both the area the preset is committed
    into and the processing the committed state compiles to.  A measurement
    from before the frozen-target era carries no context and is accepted
    unchanged, as is any commit whose area and processing are untouched.
    Single-role targets frozen before stereo-pair banks existed stay
    fail-closed: their measured_roles cover one channel only, so committing
    them into a pair bank is rejected rather than applied to both channels.
    """
    store = measurement_store
    if store is None:
        raise ValueError("Measurement store is not available")
    try:
        measurement = store.get_measurement(measurement_id)
    except KeyError as exc:
        raise ValueError(f"Source measurement {measurement_id} is no longer available") from exc
    target = measurement_target_from_context(measurement)
    if target.get("legacy"):
        return
    live = _freeze_measurement_target(target["bank_id"], _live_measurement_sample_rate())
    require_commit_target(
        target, live,
        mode=str(binding.get("mode") or ""), bank_id=str(binding.get("bank_id") or ""),
    )


def _require_state_mode(value) -> str:
    mode = str(value or "").strip()
    if mode not in MODES:
        raise HTTPException(status_code=400, detail=f"Unknown output mode: {value}")
    return mode


def _require_bank_id(value) -> str:
    bank_id = str(value or "").strip()
    if not bank_id:
        raise HTTPException(status_code=400, detail="bank_id is required")
    return bank_id


def _require_role(value) -> str:
    role = str(value or "").strip()
    if not role:
        raise HTTPException(status_code=400, detail="role is required")
    return role


def _build_output_state_mutation(mutation: dict, *, output_key: str, channels: int | None):
    if not isinstance(mutation, dict):
        raise HTTPException(status_code=400, detail="mutation must be an object")
    kind = mutation.get("kind")
    fields = {key: value for key, value in mutation.items() if key != "kind"}

    def strict(allowed: set[str]) -> dict:
        unknown = set(fields) - allowed
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown mutation fields: {sorted(unknown)}")
        return fields

    if kind == "set_routing":
        args = strict({"mode", "assignments"})
        mode = _require_state_mode(args.get("mode"))
        assignments = args.get("assignments")
        return lambda state: set_mode_routing(state, mode, output_key, assignments)
    if kind == "switch_mode":
        args = strict({"mode"})
        mode = _require_state_mode(args.get("mode"))
        return lambda state: switch_mode(state, mode)
    if kind == "set_crossover":
        args = strict({"mode", "enabled"})
        mode = _require_state_mode(args.get("mode"))
        return lambda state: set_crossover(state, mode, args.get("enabled"))
    if kind == "set_subwoofers":
        args = strict({"mode", "frequency_hz", "main_highpass_enabled", "processing",
                       "family", "slope_db_oct", "sub_link", "sub_filters"})
        mode = _require_state_mode(args.get("mode"))

        def update_subwoofers(state):
            topology = _output_state_topology(state, mode, output_key, channels)
            processing = args.get("processing")
            if not isinstance(processing, dict) or set(processing) != set(topology["sub_roles"]):
                raise ValueError("Sub settings must describe exactly the routed sub roles")
            result = set_bass_management(
                state, mode, frequency_hz=args.get("frequency_hz"),
                main_highpass_enabled=args.get("main_highpass_enabled"),
                family=args.get("family"), slope_db_oct=args.get("slope_db_oct"),
                sub_link=args.get("sub_link"), sub_filters=args.get("sub_filters"))
            for role, settings in processing.items():
                if not isinstance(settings, dict) or set(settings) != {"level_db", "alignment_ms", "polarity"}:
                    raise ValueError("Sub settings require level, alignment and polarity")
                result = set_output_processing(result, mode, role, **settings)
            return result
        return update_subwoofers
    if kind == "select_bank":
        args = strict({"mode", "bank_id"})
        if channels is None:
            raise HTTPException(status_code=400, detail="Output channel capacity is unknown")
        mode = _require_state_mode(args.get("mode"))
        bank_id = _require_bank_id(args.get("bank_id"))
        return lambda state: select_bank(state, mode, output_key, channels, bank_id)
    if kind == "set_bank_preset":
        args = strict({"mode", "bank_id", "preset", "preset_a", "preset_b", "active_side"})
        mode = _require_state_mode(args.get("mode"))
        bank_id = _require_bank_id(args.get("bank_id"))
        options = {name: args[name] for name in ("preset", "preset_a", "preset_b", "active_side")
                   if name in args}

        def update_bank(state):
            topology = _output_state_topology(state, mode, output_key, channels)
            definition = resolve_bank(state["modes"][mode], bank_id, topology["roles"])
            for name in ("preset", "preset_a", "preset_b"):
                if options.get(name) is not None:
                    try:
                        get_output_service().validate_bank_preset(state, mode, definition["id"], options[name], roles=topology["roles"])
                    except FileNotFoundError as exc:
                        raise ValueError(f"Unknown preset {options[name]!r}") from exc
            return set_bank_preset(state, mode, definition["id"], roles=topology["roles"], **options)
        return update_bank
    if kind == "switch_all_banks":
        args = strict({"mode", "active_side"})
        mode = _require_state_mode(args.get("mode"))
        if channels is None:
            raise HTTPException(status_code=400, detail="Output channel capacity is unknown")
        return lambda state: switch_all_banks(state, mode, output_key, channels, args.get("active_side"))
    if kind == "set_processing":
        args = strict({"mode", "role", "highpass", "lowpass", "level_db",
                       "alignment_ms", "polarity"})
        mode = _require_state_mode(args.get("mode"))
        role = _require_role(args.get("role"))
        options = {name: args[name] for name in ("highpass", "lowpass") if name in args}
        options.update({name: args[name] for name in ("level_db", "alignment_ms", "polarity")
                        if args.get(name) is not None})
        return lambda state: set_output_processing(state, mode, role, **options)
    if kind == "set_extras":
        args = strict({"mode", "extras"})
        mode = _require_state_mode(args.get("mode"))
        if not isinstance(args.get("extras"), dict):
            raise HTTPException(status_code=400, detail="extras must be an object")
        return lambda state: set_mode_extras(state, mode, args["extras"])
    raise HTTPException(status_code=400, detail=f"Unknown mutation kind: {kind}")


def _live_topology_key(state: dict, *, output_key: str, channels: int | None,
                       rate: int | None) -> tuple:
    """Physical-graph identity for the live fast-path gate.

    Equal keys mean the engine's port graph stays valid: only DSP-internal
    coefficients, trims and bank content change. Anything else (roles,
    wiring, channel count, rate) needs the coordinator path.
    """
    mode = state.get("active_mode") if isinstance(state, dict) else None
    assignments = routing_for_device(state, mode, output_key) if mode else []
    return (mode, state["modes"][mode]["crossover_enabled"], tuple(assignments[:channels or 0]), channels, rate)


def _plan_transition_guard(old_layout, new_layout, previous_gain: float) -> float:
    """Guard pin for a plan rebuild, mirroring the legacy mode guard."""
    current_peak = max((float(channel.get("gain_db", 0.0)) for channel in old_layout),
                       default=0.0)
    target_peak = max((float(channel.get("gain_db", 0.0)) for channel in new_layout),
                      default=0.0)
    positive_gain_delta = max(0.0, target_peak - current_peak)
    return min(0.0, float(previous_gain or 0.0) - max(1.0, positive_gain_delta + 1.0))


def _build_plan_target(service, manager, plan, *, output_key: str, rate: int,
                       hardware_ports: list, fingerprint: str | None,
                       extras_override: dict | None = None):
    """Render one plan to a runtime sync target (pre-commit, may raise).

    The global DSP chain (limiter, loudness, ...) always renders from the
    manager's global extras -- the surface the UI writes through
    /api/dsp/extras -- so the same helpers stay live on v2 crossover/bank
    graphs.  An explicit override (candidate/previous extras during a
    guarded transition) renders that snapshot instead.
    """
    layout = service.compile_layout(plan)
    config = DSPRuntimeConfig.from_plan(
        plan, layout=layout, output_key=output_key, sample_rate_hz=rate,
        hardware_ports=hardware_ports, plan_fingerprint=fingerprint)
    if extras_override is None:
        extras_override = manager.load_global_extras()
    text = manager.compile_engine_text(
        [dict(entry) for entry in layout], preset_name=plan["global"]["preset"],
        sample_rate_hz=rate, extras_override=extras_override)
    return PlannedSyncTarget(config=config, text=text)


async def _try_render_v2_sync_target(rate: int, overview: dict):
    """Render the committed v2 head for a runtime sync, or None for legacy.

    Used by the DSP orchestrator so every helper (re)build -- startup,
    playback transitions, link-watch repairs -- serves the authoritative
    output state instead of the legacy overview graph.  Any failure (no
    device, undiscovered ports, a draft that cannot activate) returns None
    and the caller keeps the legacy overview sync, so unmigrated states are
    byte-for-byte unchanged.
    """
    try:
        if not isinstance(rate, int) or rate <= 0:
            return None
        if not isinstance(overview, dict):
            return None
        service = get_output_service()
        try:
            output_key, channels = _output_state_device(overview)
        except HTTPException:
            return None
        if not channels:
            return None
        ports = list((overview.get("output_mode") or {}).get("hardware_playback_ports") or [])
        if not ports:
            return None
        try:
            state = service.load()
        except ValueError:
            return None
        try:
            plan = await asyncio.to_thread(
                service.compile_plan, state, output_key=output_key,
                channels=channels, sample_rate_hz=rate)
        except (FileNotFoundError, ValueError):
            return None
        fingerprint = service.fingerprint_plan(plan)
        manager = _require_dsp_manager()
        try:
            target = await asyncio.to_thread(
                _build_plan_target, service, manager, plan,
                output_key=output_key, rate=rate,
                hardware_ports=list(ports), fingerprint=fingerprint)
        except (RuntimeError, ValueError):
            return None
        logger.info("DSP sync serving v2 plan: mode=%s rate=%s fingerprint=%s",
                    plan.get("mode"), rate, (fingerprint or "")[:12])
        return target
    except Exception as exc:
        logger.warning("V2 plan render for DSP sync failed, using legacy overview: %s", exc)
        return None


def _stage_bank_v2_context(*, measurement_bank: str, measurement_rate_hz: int) -> dict | None:
    """Stage the committed v2 plan context for one manual bank measurement.

    Returns {"expected_native_layout", "expected_native_output_mode",
    "expected_plan_fingerprint"} compiled at the measurement rate, or None
    when the head cannot activate (the caller keeps the legacy route, whose
    pre-sweep check then fails closed as before).  Never raises.
    """
    try:
        if not str(measurement_bank or "").strip():
            return None
        if not isinstance(measurement_rate_hz, int) or measurement_rate_hz <= 0:
            return None
        service = get_output_service()
        overview = get_audio_output_overview()
        try:
            output_key, channels = _output_state_device(overview)
        except HTTPException:
            return None
        if not channels:
            return None
        state = service.load()
        plan = service.compile_plan(state, output_key=output_key, channels=channels,
                                    sample_rate_hz=measurement_rate_hz)
        layout = service.compile_layout(plan)
        return {"expected_native_layout": [dict(entry) for entry in layout],
                "expected_native_output_mode": plan["mode"],
                "expected_plan_fingerprint": service.fingerprint_plan(plan)}
    except Exception as exc:
        logger.warning("Bank v2 staging failed for measurement: %s", exc)
        return None


async def _sync_v2_head_after_bank_assign() -> dict:
    """Best-effort live sync after a bank preset assignment (import flows).

    Import/create endpoints persist through OutputService without touching
    the runtime; without this the engine keeps serving the previous bank
    content until the next unrelated edit.  Never raises: the assignment
    stays committed when the head cannot activate or the sync fails.
    """
    try:
        overview = await asyncio.to_thread(get_audio_output_overview)
        try:
            output_key, channels = _output_state_device(overview)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else "No audio output device"
            return {"live_applied": False, "live_reason": detail}
        status = get_samplerate_status()
        rate = status.get("active_rate")
        if not isinstance(rate, int) or rate <= 0:
            rate = status.get("force_rate")
        if not isinstance(rate, int) or rate <= 0 or not channels:
            return {"live_applied": False, "live_reason": "rate-unknown"}
        if runtime.dsp_runtime is None:
            return {"live_applied": False, "live_reason": "dsp-runtime-unavailable"}
        target = await _try_render_v2_sync_target(rate, overview)
        if target is None:
            return {"live_applied": False, "live_reason": "not-activatable"}
        await runtime.dsp_runtime.sync_rendered(target)
        return {"live_applied": True, "live_reason": None}
    except Exception as exc:
        logger.warning("Bank-assign live sync failed: %s", exc)
        return {"live_applied": False, "live_reason": "live-apply-failed"}


def _create_autosub_release_adapter(*, service, output_key: str, channels: int):
    """Compose a release adapter for one committed AutoSub device context.

    The adapter renders the current committed output plan at the restore
    rate when the measurement session releases.  Ports are discovery data
    resolved at composition; the plan itself always renders live from the
    current head, so a deferred release never serves stale content.  The
    device context (output key/channels/ports) stays pinned: only the
    committing device's release may consume the adapter.  The live
    selection is re-resolved at invoke time: a device-switched orphan
    refuses instead of rebuilding the pinned graph over the new device.
    """
    from measurement.autosub.release import create_release_adapter
    manager = _require_dsp_manager()
    overview = get_audio_output_overview()
    ports = list((overview.get("output_mode") or {}).get("hardware_playback_ports") or [])
    return create_release_adapter(
        service=service, dsp_manager=manager, hardware_ports=ports,
        get_native_runtime=lambda: runtime.dsp_runtime,
        output_key=output_key, channels=channels,
        resolve_live_device=_live_release_device_context)


def _create_auto_sub_candidate_session(*, service, start_state: dict, output_key: str,
                                       channels: int, hardware_ports: list):
    """Compose an inert owner, pinning one runtime and discovered device context."""
    from measurement.autosub.candidate_session import AutoSubCandidateSession

    native_runtime = runtime.dsp_runtime
    if native_runtime is None:
        raise RuntimeError("Native DSP runtime is unavailable")
    manager = _require_dsp_manager()
    ports = list(hardware_ports)
    if len(ports) < channels:
        raise ValueError("AutoSub output has insufficient discovered playback ports")

    def require_runtime():
        if runtime.dsp_runtime is not native_runtime:
            raise RuntimeError("AutoSub native runtime ownership changed")

    def build_target(plan, *, fingerprint):
        require_runtime()
        return _build_plan_target(
            service, manager, plan, output_key=output_key, rate=plan["sample_rate_hz"],
            hardware_ports=ports, fingerprint=fingerprint)

    async def guarded_stage(*args, **kwargs):
        require_runtime()
        await native_runtime.guarded_rebuild_rendered(*args, **kwargs)
        require_runtime()

    async def readback():
        require_runtime()
        before = native_runtime.snapshot()
        links_valid = await native_runtime.verify()
        require_runtime()
        after = native_runtime.snapshot()
        config = after.get("config") or {}
        same_graph = (before.get("helper_pid") == after.get("helper_pid")
                      and before.get("config") == config)
        expected_device = (config.get("output_key") == output_key
                           and config.get("hardware_ports") == ports)
        if (not links_valid or not same_graph or not expected_device
                or before.get("active") is not True or after.get("active") is not True
                or not after.get("helper_pid")):
            raise RuntimeError("AutoSub runtime links, device or process identity could not be verified")
        return after

    return AutoSubCandidateSession(
        service=service, start_state=start_state, output_key=output_key, channels=channels,
        build_target=build_target, guarded_stage=guarded_stage, readback=readback)


_speaker_align_service_instance = None


def _describe_speaker_align_device(state: dict) -> dict:
    """Resolve the selected output device context for one speaker job.

    Mirrors the measurement-target device resolution: the committed
    overview's effective key/channels plus discovered playback ports.
    Raises ValueError (the service maps it to HTTP 400) instead of the
    HTTPException the interactive output-state routes use.
    """
    del state
    overview = get_audio_output_overview()
    try:
        output_key, channels = _output_state_device(overview)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "No audio output device is selected"
        raise ValueError(detail) from exc
    ports = list((overview.get("output_mode") or {}).get("hardware_playback_ports") or [])
    return {"output_key": output_key, "channels": int(channels or 0),
            "hardware_ports": ports}


def _live_release_device_context() -> dict:
    """Resolve the live output device selection for release validation.

    Same shape as the pinned adapter context: output key, channel count
    and discovered playback ports. Raises RuntimeError when no output is
    selected, so an orphaned release adapter fails closed instead of
    rebuilding a stale device graph.
    """
    overview = get_audio_output_overview()
    try:
        output_key, channels = _output_state_device(overview)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "No audio output device is selected"
        raise RuntimeError(detail) from exc
    ports = list((overview.get("output_mode") or {}).get("hardware_playback_ports") or [])
    return {"output_key": output_key, "channels": int(channels or 0),
            "hardware_ports": ports}


def _create_speaker_align_release_adapter(*, service, output_key: str, channels: int):
    """Compose a release adapter for one committed Speaker Align device context.

    The adapter renders the current committed output plan at the restore
    rate when the measurement session releases. Ports are discovery data
    resolved at composition; the plan itself always renders live from the
    current head, so a deferred release never serves stale content. The
    live selection is re-resolved at invoke time: a device-switched orphan
    refuses instead of rebuilding the pinned graph over the new device.
    """
    from measurement.speaker_commit import create_speaker_release_adapter
    manager = _require_dsp_manager()
    overview = get_audio_output_overview()
    ports = list((overview.get("output_mode") or {}).get("hardware_playback_ports") or [])
    return create_speaker_release_adapter(
        service=service, dsp_manager=manager, hardware_ports=ports,
        get_native_runtime=lambda: runtime.dsp_runtime,
        output_key=output_key, channels=channels,
        resolve_live_device=_live_release_device_context)


def _speaker_align_input_keeper(job_id: str, params: dict):
    """Hold the job's mic source open for the whole alignment run.

    The keeper tap keeps the capture device streaming between way captures
    so no suspend/resume cycle can slip the mic timing mid-run. Returned as
    an async context manager for the service's keeper scope.
    """
    from measurement.input_keeper import input_keeper_scope
    return input_keeper_scope(
        measurement_store, input_id=str(params.get("input_id") or ""),
        mic_input_channel=params.get("mic_input_channel", "1"),
        owner=f"speaker-align-{job_id}")


def get_speaker_align_service():
    """Return the Speaker Align application service (late-bound singleton).

    The measurement store value is captured at first use, which always
    post-dates lifespan startup in production; tests rebind through their
    own factory instead of this singleton.
    """
    global _speaker_align_service_instance
    if _speaker_align_service_instance is None:
        _speaker_align_service_instance = speaker_api.build_speaker_align_service(
            output_service=get_output_service(),
            measurement_store=measurement_store,
            dsp_manager=_require_dsp_manager(),
            get_native_runtime=lambda: runtime.dsp_runtime,
            describe_device=_describe_speaker_align_device,
            get_measurement_rate=_live_measurement_sample_rate,
            get_measurement_session=lambda: measurement_sr_session,
            prepare_measurement=measurement_session._measurement_entry_preflight,
            another_measurement_active=autosub.is_optimization_active,
            build_release_adapter=lambda *, output_key, channels: _create_speaker_align_release_adapter(
                service=get_output_service(), output_key=output_key, channels=channels),
            input_keeper=_speaker_align_input_keeper)
    return _speaker_align_service_instance


async def _sync_plan_runtime(target, *, reason: str = "output-state-transition") -> None:
    """Stage a prebuilt plan target on the native runtime (coordinator use)."""
    del reason
    if isinstance(target, Mapping):
        target = PlannedSyncTarget(config=target["config"], text=target["text"])
    if runtime.dsp_runtime is None:
        raise RuntimeError("Native DSP runtime is unavailable")
    await runtime.dsp_runtime.sync_rendered(target)


def _output_state_topology(state: dict, mode: str, output_key: str, channels: int | None) -> dict:
    topology = derive_topology(mode, routing_for_device(state, mode, output_key), channels=channels,
                               crossover_enabled=state["modes"][mode]["crossover_enabled"])
    return {"mode": topology.mode, "crossover_enabled": topology.crossover_enabled, "roles": list(topology.roles),
            "sub_roles": list(topology.sub_roles), "sub_mode": topology.sub_mode,
            "left_ways": list(topology.left_ways), "right_ways": list(topology.right_ways),
            "way_count": topology.way_count, "issues": list(topology.issues)}


@app.get("/api/audio/output-state")
async def get_audio_output_state():
    service = get_output_service()
    try:
        state = service.ensure_state()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"Output state is unavailable: {exc}")
    overview = await asyncio.to_thread(get_audio_output_overview)
    output_key, channels = _output_state_device(overview)
    modes = {}
    for mode, config in state["modes"].items():
        topology = _output_state_topology(state, mode, output_key, channels)
        banks = bank_catalog(config, topology["roles"])
        modes[mode] = {
            "crossover_enabled": config["crossover_enabled"],
            "selected_bank": selected_bank(config, topology["roles"]),
            "banks": banks,
            "all_banks": summarize_banks([config["banks"][role] for role in topology["roles"]]),
            "processing": config["processing"],
            "bass_management": config["bass_management"],
            "extras": config["extras"],
            "topology": topology,
        }
    return {
        "status": "ok",
        "revision": state["revision"],
        "active_mode": state["active_mode"],
        "device": {
            "key": output_key,
            "channels": channels,
            "routing": {mode: routing_for_device(state, mode, output_key)
                        for mode in MODES},
        },
        "modes": modes,
        "capabilities": {
            "modes": list(MODES),
            "roles": {mode: list(roles_for_mode(mode, crossover_enabled=state["modes"][mode]["crossover_enabled"]))
                      for mode in MODES},
            "filter_families": {family: list(slopes) for family, slopes in FILTER_SLOPES.items()},
            "max_slope_db_oct": 72,
            "max_biquads_per_output": DSPManager.OUTPUT_FILTER_MAX_BIQUADS,
        },
    }


@app.post("/api/audio/output-state/apply")
async def apply_audio_output_state(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid JSON body, expected {"expected_revision": <int>, "mutation": {...}}')
    return await _apply_audio_output_state_body(body)


async def _apply_audio_output_state_body(body: dict):
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail='Invalid JSON body, expected {"expected_revision": <int>, "mutation": {...}}')
    expected_revision = body.get("expected_revision")
    if type(expected_revision) is bool or not isinstance(expected_revision, int) or expected_revision < 0:
        raise HTTPException(status_code=400, detail="expected_revision must be a non-negative integer")
    if measurement_sr_session is not None and measurement_sr_session.has_active_jobs:
        raise HTTPException(status_code=423, detail="Measurement is active; output state is locked")

    service = get_output_service()
    try:
        state = service.ensure_state()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"Output state is unavailable: {exc}")
    if state["revision"] != expected_revision:
        raise HTTPException(status_code=409, detail={
            "code": "revision-conflict", "message": "Output state changed; refresh before applying",
            "revision": state["revision"]})
    overview = await asyncio.to_thread(get_audio_output_overview)
    output_key, channels = _output_state_device(overview)
    mutate = _build_output_state_mutation(
        body.get("mutation"), output_key=output_key, channels=channels)
    try:
        candidate = mutate(state)
    except ValueError as exc:
        raise bad_request(exc)

    status = get_samplerate_status()
    target_rate = status.get("active_rate")
    if not isinstance(target_rate, int) or target_rate <= 0:
        target_rate = status.get("force_rate")
    live_known = isinstance(target_rate, int) and target_rate > 0 and bool(channels)
    old_plan = new_plan = None
    fingerprints: dict[str, str | None] = {"old": None, "new": None}
    if live_known:
        # A draft that cannot activate (incomplete routing, unresolvable
        # bank preset) has no meaningful plan or fingerprint; the commit
        # below still persists it, activation gates on compilability later.
        for key, document in (("old", state), ("new", candidate)):
            try:
                plan = service.compile_plan(
                    document, output_key=output_key, channels=channels,
                    sample_rate_hz=target_rate)
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
        manager = _require_dsp_manager()
        try:
            new_target = _build_plan_target(service, manager, new_plan, output_key=output_key,
                                            rate=target_rate, hardware_ports=list(ports),
                                            fingerprint=new_fp)
            old_target = None
            if old_plan is not None:
                old_target = _build_plan_target(service, manager, old_plan, output_key=output_key,
                                                rate=target_rate, hardware_ports=list(ports),
                                                fingerprint=old_fp)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=f"Planned output cannot stage: {exc}")
        live_overview = copy.deepcopy(overview)
        live_overview["output_mode"] = {
            **(live_overview.get("output_mode") or {}),
            "mode": new_plan["mode"],
            "planned_routes": [[signal, port] for signal, port in new_target.config.route_pairs],
            "output_state_revision": "pending",
        }
        if service.load()["revision"] != expected_revision:
            raise HTTPException(status_code=409, detail={
                "code": "revision-conflict", "message": "Output state changed during transition prepare",
                "revision": service.load()["revision"]})
        context = await _coordinator_current_playback_context()
        try:
            await _run_coordinated_transition(TransitionRequest(
                operation="output-mode-switch",
                source=str(context.get("source") or "local"),
                target_rate=target_rate,
                target_url=context.get("target_url"),
                target_track=dict(context.get("target_track") or {}),
                should_play=bool(context.get("should_play")),
                rate_change=False,
                reload_source=False,
                detail="api-audio-output-state",
                output_mode_target=live_overview,
                output_state_transition={
                    "candidate_state": candidate,
                    "previous_state": state,
                    "expected_revision": expected_revision,
                    "fingerprint": new_fp,
                    "target": new_target,
                    "previous_target": old_target,
                    "output_key": output_key,
                    "channels": channels,
                }))
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
            raise _transition_error_http(exc) from exc
        committed = service.load()
        return {
            "status": "ok",
            "revision": committed["revision"],
            "active_mode": committed["active_mode"],
            "fingerprint": new_fp,
            "fingerprint_changed": True,
            "live_applied": True,
            "live_reason": None,
            "topology": _output_state_topology(committed, committed["active_mode"], output_key, channels),
        }

    # Stage render targets BEFORE committing: a candidate that compiles but
    # cannot render (unstable coefficients, unresolvable IR, missing ports)
    # must fail here with the last good head still live -- never as a
    # committed-but-unrunnable poison head that every later background sync
    # trips over (and silently replaces with the legacy graph).
    staged_new_target = staged_old_target = None
    staged_guard = 0.0
    if (live_known and new_plan is not None and old_fp != new_fp
            and runtime.dsp_runtime is not None
            and (overview.get("output_mode") or {}).get("hardware_playback_ports")):
        ports = list((overview.get("output_mode") or {}).get("hardware_playback_ports") or [])
        manager = _require_dsp_manager()
        try:
            staged_new_target = _build_plan_target(
                service, manager, new_plan, output_key=output_key,
                rate=target_rate, hardware_ports=ports, fingerprint=new_fp)
            if old_plan is not None:
                staged_old_target = _build_plan_target(
                    service, manager, old_plan, output_key=output_key,
                    rate=target_rate, hardware_ports=ports, fingerprint=old_fp)
        except (RuntimeError, ValueError) as exc:
            logger.warning("Output-state staging failed before commit (head unchanged): %s", exc)
            raise HTTPException(status_code=500, detail=f"Planned output cannot stage: {exc}")
        if staged_old_target is not None:
            previous_gain = float((runtime.dsp_runtime.snapshot() or {}).get("output_gain_db") or 0.0)
            staged_guard = _plan_transition_guard(
                staged_old_target.config.layout, staged_new_target.config.layout, previous_gain)

    try:
        # A measurement job may have taken the graph during the awaits above.
        committed = service.commit_unowned(candidate, expected_revision=expected_revision)
    except StateConflictError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "revision-conflict", "message": str(exc),
            "revision": service.load()["revision"]}) from exc
    except MeasurementActiveError as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    except ValueError as exc:
        raise bad_request(exc)

    def draft_response(reason: str) -> dict:
        return {
            "status": "ok",
            "revision": committed["revision"],
            "active_mode": committed["active_mode"],
            "fingerprint": new_fp,
            "fingerprint_changed": None if old_fp is None or new_fp is None else old_fp != new_fp,
            "live_applied": False,
            "live_reason": reason,
            "topology": _output_state_topology(committed, committed["active_mode"], output_key, channels),
        }

    if not live_known:
        return draft_response("rate-unknown" if not target_rate else "output-capacity-unknown")
    if new_plan is None:
        return draft_response("not-activatable")
    if staged_new_target is None:
        if old_fp is not None and old_fp == new_fp:
            return draft_response("nothing-to-apply")
        if runtime.dsp_runtime is None:
            return draft_response("dsp-runtime-unavailable")
        return draft_response("output-ports-undiscovered")
    if staged_old_target is None:
        # The stored head becomes activatable with this commit (e.g. the
        # missing crossover starter filters were just supplied).  There is no
        # previous valid graph to guard against or roll back to: sync the new
        # plan directly.  Returning "not-activatable" here would persist the
        # valid head while leaving the stale engine behind, so every later
        # edit looks like the first audible change.
        try:
            await runtime.dsp_runtime.sync_rendered(staged_new_target)
        except BaseException as exc:
            try:
                service.revert(state, expected_revision=committed["revision"])
            except BaseException:
                logger.exception("Output-state activation rollback failed")
            logger.warning("Output-state activation failed after commit: %s", exc)
            raise HTTPException(status_code=500, detail={
                "code": "live-apply-failed", "message": str(exc),
                "revision": service.load()["revision"],
                "rollback": "committed"}) from exc
        return {
            "status": "ok",
            "revision": committed["revision"],
            "active_mode": committed["active_mode"],
            "fingerprint": new_fp,
            "fingerprint_changed": True,
            "live_applied": True,
            "live_reason": None,
            "topology": _output_state_topology(committed, committed["active_mode"], output_key, channels),
        }
    try:
        await runtime.dsp_runtime.guarded_rebuild_rendered(
            staged_new_target, previous=staged_old_target, guard_db=staged_guard,
            apply_candidate=lambda: None, apply_previous=lambda: None,
            settle_seconds=0.0)
    except BaseException as exc:
        rolled_back = False
        try:
            service.revert(state, expected_revision=committed["revision"])
            rolled_back = True
            await runtime.dsp_runtime.sync_rendered(staged_old_target, initial_output_gain_db=staged_guard)
        except BaseException:
            logger.exception("Output-state fast-path rollback failed")
        logger.warning("Output-state live apply failed (rollback=%s): %s",
                       "committed" if rolled_back else "conflicted", exc)
        raise HTTPException(status_code=500, detail={
            "code": "live-apply-failed", "message": str(exc),
            "revision": service.load()["revision"] if rolled_back else committed["revision"],
            "rollback": "committed" if rolled_back else "conflicted"}) from exc
    return {
        "status": "ok",
        "revision": committed["revision"],
        "active_mode": committed["active_mode"],
        "fingerprint": new_fp,
        "fingerprint_changed": True,
        "live_applied": True,
        "live_reason": None,
        "topology": _output_state_topology(committed, committed["active_mode"], output_key, channels),
    }


def _crossover_bass_highpass(mode_config: dict, topology: dict, role: str) -> dict | None:
    """Sub crossover high-pass the DSP adds on top of stored way filters.

    Mirrors ``dsp.processing_plan._crossover_filters``: with routed subs and
    ``main_highpass_enabled`` every speaker way runs through the sub
    crossover, its type and slope included. A true Stereo sub pair resolves
    per side while unlinked; Mono and Dual-Mono run the shared crossover.
    The speaker tile must show the same curve, so the response endpoint
    reuses this definition.
    """
    bass = (mode_config or {}).get("bass_management") or {}
    sub_roles = (topology or {}).get("sub_roles") or []
    if not sub_roles or bass.get("main_highpass_enabled") is not True:
        return None
    try:
        definition = (bass_crossover_for_side(bass, side_for_role(role))
                      if (topology or {}).get("sub_mode") == "stereo"
                      else shared_bass_crossover(bass))
        frequency = float(definition["frequency_hz"])
    except (TypeError, ValueError, KeyError):
        return None
    if not 40 <= frequency <= 200:
        return None
    return {"family": definition["family"], "slope_db_oct": definition["slope_db_oct"],
            "frequency_hz": int(round(frequency))}


def _crossover_way_required(role: str) -> tuple[str, ...]:
    """Directions a crossover way defines: Low its low-pass, High its high-pass."""
    if role.endswith("low"):
        return ("lowpass",)
    if role.endswith("high"):
        return ("highpass",)
    return ("highpass", "lowpass")


def _crossover_way_complete(role: str, role_settings: dict) -> bool:
    """Whether every direction of the way is stored (any Off direction is not)."""
    return all(role_settings.get(kind) is not None for kind in _crossover_way_required(role))


def _crossover_way_points(role: str, role_settings: dict, sample_rate_hz: int,
                          point_count: int = 180, extra_highpass: dict | None = None) -> list | None:
    """Evaluate one way's crossover filters to log-spaced magnitude points.

    Returns None only when a stored filter cannot be evaluated. A cleared
    (Off) direction is a valid operating state and simply contributes no
    filter, so the curve shows the band the way actually runs; whether all
    required directions are set is reported separately as ``complete``.
    Only crossover filters shape this curve; area-bank PEQ/FIR correction is
    visualized in the measurement graph instead. ``extra_highpass`` carries
    the shared bass high-pass from the subwoofer tile; it never satisfies a
    stored way filter, it only shapes the running curve.
    """
    try:
        sections = []
        for kind in ("highpass", "lowpass"):
            definition = role_settings.get(kind)
            if definition is None:
                continue
            sections.extend(design_crossover(
                {"kind": kind, "family": definition["family"],
                 "slope_db_oct": definition["slope_db_oct"],
                 "frequency_hz": definition["frequency_hz"]}, sample_rate_hz))
        if extra_highpass is not None:
            sections.extend(design_crossover(
                {"kind": "highpass", "family": extra_highpass["family"],
                 "slope_db_oct": extra_highpass["slope_db_oct"],
                 "frequency_hz": extra_highpass["frequency_hz"]}, sample_rate_hz))
    except (ValueError, KeyError, TypeError):
        return None
    points = []
    for index in range(point_count):
        frequency = 20.0 * (20000.0 / 20.0) ** (index / (point_count - 1))
        total = 1.0 + 0.0j
        for section in sections:
            b0, b1, b2, _, a1, a2 = section
            z = cmath.exp(2j * math.pi * frequency / sample_rate_hz)
            total *= (b0 + b1 / z + b2 / z / z) / (1.0 + a1 / z + a2 / z / z)
        points.append([round(frequency, 3), round(20.0 * math.log10(abs(total)), 3)])
    return points


@app.get("/api/audio/output-state/crossover-response")
async def get_audio_output_state_crossover_response():
    service = get_output_service()
    try:
        state = service.ensure_state()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"Output state is unavailable: {exc}")
    status = get_samplerate_status()
    rate = status.get("active_rate")
    if not isinstance(rate, int) or rate <= 0:
        rate = status.get("force_rate")
    if not isinstance(rate, int) or rate <= 0:
        rate = 48000
    mode = state["active_mode"]
    ways = {}
    overview = await asyncio.to_thread(get_audio_output_overview)
    output_key, channels = _output_state_device(overview)
    topology = _output_state_topology(state, mode, output_key, channels)
    mode_config = state["modes"][mode]
    for role, settings in mode_config["processing"].items():
        if not topology["crossover_enabled"] or role not in topology["roles"]:
            continue
        if not (role.startswith("left_") or role.startswith("right_")):
            continue
        bass_highpass = _crossover_bass_highpass(mode_config, topology, role)
        points = _crossover_way_points(role, settings, rate, extra_highpass=bass_highpass)
        ways[role] = {
            "filters": {"highpass": settings["highpass"], "lowpass": settings["lowpass"]},
            "derived_highpass": dict(bass_highpass) if bass_highpass else None,
            "complete": _crossover_way_complete(role, settings),
            "points": points,
        }
    return {"status": "ok", "revision": state["revision"], "mode": mode,
            "crossover_enabled": topology["crossover_enabled"],
            "bass_management": dict(mode_config["bass_management"]),
            "sub_roles": list(topology["sub_roles"]),
            "sample_rate_hz": rate, "ways": ways}


@app.post("/api/debug/21-runtime-state")
async def debug_21_runtime_state_route(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    label = str(body.get("label") or "manual").strip() if isinstance(body, dict) else "manual"
    ui_state = body.get("ui_state") if isinstance(body, dict) and isinstance(body.get("ui_state"), dict) else {}
    return await _dump_21_runtime_state(label, ui_state)


@app.get("/api/audio/source-mode")
async def audio_source_overview():
    return get_audio_source_overview()


@app.get("/api/audio/bluetooth")
async def audio_bluetooth_overview():
    return get_bluetooth_audio_overview()


def _source_pause_baseline() -> dict[str, Any]:
    """Capture the app-commit boundary the source pause must not overtake.

    The pause stops whatever app transport is live, unless a newer app commit
    took ownership afterwards: its commit id differs from the baseline while
    the owner names an MPV source (local/radio/tidal) or the paused provider
    itself. A newer source switch aborts the whole pause; the newer switch
    owns pausing from its own baseline.
    """
    return {
        "commit_id": playback_state.playback_context_commit_id,
        "owner": playback_state.current_playback_owner,
        "source_generation": playback_state.source_generation,
    }


def _source_pause_superseded(baseline: dict[str, Any]) -> bool:
    """Return whether a newer source switch started during the pause."""
    return playback_state.source_generation != baseline.get("source_generation")


def _mpv_pause_superseded(baseline: dict[str, Any]) -> bool:
    if _source_pause_superseded(baseline):
        return True
    return (
        playback_state.playback_context_commit_id != baseline.get("commit_id")
        and source_policy.is_mpv_source(playback_state.current_playback_owner)
    )


def _provider_pause_superseded(baseline: dict[str, Any], provider: str) -> bool:
    if _source_pause_superseded(baseline):
        return True
    return (
        playback_state.playback_context_commit_id != baseline.get("commit_id")
        and playback_state.current_playback_owner == provider
    )


def _coordinator_lock_for_source_pause() -> asyncio.Lock | None:
    coordinator = playback_transition_coordinator
    lock = getattr(coordinator, "lock", None)
    return lock if isinstance(lock, asyncio.Lock) else None


async def _pause_all_app_playback_for_external_input() -> None:
    # Baseline is captured after the source generation bump, so every app
    # commit that finished before the pause is stopped, while a commit that
    # lands during the pause is left alone. The Coordinator lock serializes
    # the pause against in-flight transitions: a stale one is discarded by
    # its source checkpoints, a new one commits first and is then skipped by
    # the per-source checks below.
    baseline = _source_pause_baseline()
    lock = _coordinator_lock_for_source_pause()
    if lock is not None:
        async with lock:
            await _pause_scoped_app_playback(baseline)
    else:
        await _pause_scoped_app_playback(baseline)


async def _pause_scoped_app_playback(baseline: dict[str, Any]) -> None:
    try:
        if _source_pause_superseded(baseline):
            return
        if runtime.player_instance and runtime.player_instance._running and not _mpv_pause_superseded(baseline):
            await _drain_worker(runtime.player_instance.stop_playback)
            await manager.broadcast({"type": "playback", "data": build_playback_payload(runtime.player_instance.state)})
            released = await media_readiness.wait_for_pipewire_mpv_release()
            if not released:
                await asyncio.sleep(SOURCE_HANDOFF_SETTLE_MS / 1000)
    except Exception as exc:
        logger.warning("Local pause for external input failed: %s", exc)
    try:
        if _source_pause_superseded(baseline):
            return
        spotify_state = await get_spotify_ui_state()
        if spotify_state.get("status") == "Playing" and not _provider_pause_superseded(baseline, "spotify"):
            data = await spotify_pause()
            await broadcast_spotify_state(data)
    except Exception as exc:
        logger.warning("Spotify pause for external input failed: %s", exc)
    try:
        if _source_pause_superseded(baseline):
            return
        qobuz_state = await get_qobuz_ui_state()
        if _is_qobuz_playback_active(qobuz_state) and not _provider_pause_superseded(baseline, "qobuz"):
            await qobuz_pause()
            await broadcast_qobuz_state()
    except Exception as exc:
        logger.warning("Qobuz pause for external input failed: %s", exc)


async def _shield_coro(coro) -> Any:
    """Drain a coroutine to its terminal state, surviving caller cancellation.

    The caller owns a critical section (here: the source-transition lock). A
    cancelled caller must not release it while the body is still mutating
    routing/playback state: the body runs to completion, then CancelledError
    is re-raised. Body exceptions propagate unchanged; on the cancellation
    path they are logged and cancellation takes precedence.
    """
    task: asyncio.Task = asyncio.create_task(coro)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        try:
            task.result()
        except BaseException:
            logger.exception("Source transition failed while its caller was cancelled")
        raise asyncio.CancelledError
    return task.result()


async def _apply_audio_source_selection(mode: str, input_key: str | None) -> dict:
    """Run one source-mode/input change: routing commit, pause, peak sync.

    Runs under the source-transition lock with caller cancellation deferred
    (see _shield_coro): routing, generations, transports and peak state are
    always left consistent, never half-committed. The source generation (and
    the playback intent generation) advances exactly when the committed
    (mode, input) selection changed, before the external/bluetooth pause, so
    in-flight playback transitions are discarded and the pause never stops a
    newer commit.
    """
    previous_source_state = samplerate._load_audio_source_selection()
    try:
        result = set_audio_source_selection(mode, input_key)
        result = await external_input.sync(result)
        result = await bluetooth_input.sync(result)
    except BaseException:
        try:
            restored = set_audio_source_selection(
                str(previous_source_state.get("mode") or SOURCE_MODE_APP_PLAYBACK),
                previous_source_state.get("selected_input_key"),
            )
            try:
                restored = await external_input.sync(restored)
                restored = await bluetooth_input.sync(restored)
            except BaseException:
                logger.exception("Failed to re-sync routing after source-mode rollback")
        except BaseException:
            logger.exception("Failed to restore previous source selection after routing failure")
        raise
    new_mode = str(result.get("mode") or SOURCE_MODE_APP_PLAYBACK)
    previous_mode = str(previous_source_state.get("mode") or SOURCE_MODE_APP_PLAYBACK)
    new_key = (result.get("selected_input") or {}).get("key") if isinstance(result.get("selected_input"), dict) else None
    if new_mode != previous_mode or (new_key or None) != (previous_source_state.get("selected_input_key") or None):
        playback_state.note_source_selection(new_mode)
    if result.get("mode") in {SOURCE_MODE_EXTERNAL_INPUT, SOURCE_MODE_BLUETOOTH_INPUT}:
        await _pause_all_app_playback_for_external_input()
    await peak_monitor_coordinator.sync_source_mode_state(result)
    return result


@app.post("/api/audio/source-mode")
async def save_audio_source_selection_route(request: Request):
    try:
        body = await request.json()
        mode = str(body.get("mode", "")).strip()
        input_key = str(body.get("inputKey", body.get("input_key", ""))).strip() or None
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid JSON body, expected {"mode": <string>, "inputKey": <string?>}')

    try:
        async with _source_transition_lock():
            return await _shield_coro(_apply_audio_source_selection(mode, input_key))
    except ValueError as exc:
        raise bad_request(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save source mode: {exc}")


def _make_preset_load_deps() -> preset_loading.PresetLoadDeps:
    """Bind the DSP preset-load coordination to the application services.

    All entries resolve the current runtime state at call time, so tests that
    patch main attributes observe the patched services through the thin
    wrappers below (same contract as the other ``configure_*`` extractions).
    """
    return preset_loading.PresetLoadDeps(
        require_dsp_manager=lambda: _require_dsp_manager(),
        get_dsp_runtime=lambda: runtime.dsp_runtime,
        get_output_volume=lambda: get_output_volume(),
        set_output_volume=lambda value: set_output_volume(value),
        drain_worker=lambda *args, **kwargs: _drain_worker(*args, **kwargs),
        dsp_mutation_lock=lambda: _dsp_mutation_lock(),
        canonical_volume_write_lock=lambda: _canonical_volume_write_lock(),
        sync_runtime=lambda *args, **kwargs: dsp_orchestrator.sync_runtime(*args, **kwargs),
    )


def _require_dsp_manager():
    """Composition-root guard for the DSP manager singleton (stays in main).

    Kept here (not in dsp.preset_loading) because it guards main's own
    global and is an established patch seam: the preset-loading module
    resolves the manager through the injected ``require_dsp_manager``
    dependency, which delegates to this guard.
    """
    global dsp_manager
    if not dsp_manager:
        raise HTTPException(status_code=503, detail="DSP manager not available")
    return dsp_manager


async def _load_dsp_preset(
    preset_name: str, *, convolver_sample_rate_hz: int | None = None,
    _locks_held: bool = False, _rate_lock_held: bool = False,
) -> None:
    """Thin wrapper: DSP preset loading lives in dsp.preset_loading (REFACTOR-016)."""
    await preset_loading._load_dsp_preset(
        preset_name, convolver_sample_rate_hz=convolver_sample_rate_hz,
        _locks_held=_locks_held, _rate_lock_held=_rate_lock_held)


async def _volume_state_for_manager(
    manager, *, live_master: int | None = None
) -> volume_contract.VolumeState:
    """Thin wrapper: DSP preset loading lives in dsp.preset_loading (REFACTOR-016)."""
    return await preset_loading._volume_state_for_manager(manager, live_master=live_master)


@app.get("/api/library/status")
async def library_status():
    scanner = runtime.music_library.scanner
    if scanner:
        return scanner.status()
    return {"scanning": False, "track_count": 0, "error": "Library scanner not initialized"}


@app.get("/api/music-libraries")
async def list_music_libraries():
    manager = runtime.music_library.manager
    if manager is None:
        raise HTTPException(status_code=503, detail="Music libraries are not initialized")
    # Stale-while-revalidate: known shares answer immediately; a stale cache
    # refreshes once in the background (single-flight) while the UI shows
    # the cached list with a scanning hint and re-fetches afterwards.
    cached = await asyncio.to_thread(manager.status_cached)
    refreshing = await _request_library_discovery_refresh(manager)
    return {**cached, "discovery_refreshing": refreshing}


async def _request_library_discovery_refresh(manager, *, force: bool = False) -> bool:
    """Trigger one background library rescan when due. Never blocks on network.

    Returns True while a scan is running afterwards (just started here or
    already in flight elsewhere), so the UI can show progress and re-fetch
    once instead of polling.
    """
    if await asyncio.to_thread(manager.claim_background_refresh, force=force):
        async def _run_claimed_refresh():
            try:
                await asyncio.to_thread(manager.run_claimed_refresh)
            except Exception:
                logger.exception("Background music library discovery refresh failed")
        _create_lifecycle_background_task(_run_claimed_refresh(), name="library-discovery-refresh")
        return True
    return await asyncio.to_thread(manager.discovery_running)


@app.post("/api/music-libraries/refresh")
async def refresh_music_libraries():
    """Explicit manual refresh: rescan once in the background, answer from cache."""
    manager = runtime.music_library.manager
    if manager is None:
        raise HTTPException(status_code=503, detail="Music libraries are not initialized")
    cached = await asyncio.to_thread(manager.status_cached)
    refreshing = await _request_library_discovery_refresh(manager, force=True)
    return {**cached, "discovery_refreshing": refreshing}


@app.post("/api/music-libraries/manual")
async def add_manual_music_library(request: Request):
    manager = runtime.music_library.manager
    if manager is None:
        raise HTTPException(status_code=503, detail="Music libraries are not initialized")
    body = await _json_object(request)
    try:
        entry = manager.add_manual_url(str(body.get("url") or ""))
    except (ValueError, TypeError) as exc:
        raise bad_request(exc) from exc
    cached = await asyncio.to_thread(manager.status_cached)
    refreshing = await _request_library_discovery_refresh(manager)
    return {"entry": entry, **cached, "discovery_refreshing": refreshing}


@app.post("/api/music-libraries/select")
async def select_music_library(request: Request):
    manager = runtime.music_library.manager
    scanner = runtime.music_library.scanner
    if manager is None or scanner is None:
        raise HTTPException(status_code=503, detail="Music libraries are not initialized")
    try:
        body = await request.json()
        library_id = str(body.get("id") or "")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    async with _music_library_lock():
        if _playback_transition_is_active():
            raise HTTPException(status_code=409, detail="A playback transition is in progress")
        try:
            root = await asyncio.to_thread(manager.activate, library_id)
        except (ValueError, FileNotFoundError) as exc:
            raise bad_request(exc) from exc
        if root == scanner.music_root:
            # Cached response: the selector entry was already known, so no
            # network rescan belongs into this answer path.
            return manager.status_cached()
        scanner.cancel_refresh()
        active_refreshes = [task for task in runtime.library_refresh_tasks if not task.done()]
        if active_refreshes:
            await asyncio.gather(*active_refreshes, return_exceptions=True)
        if runtime.player_instance is not None and runtime.player_instance._running:
            _mark_playback_intent_changed()
            await _drain_worker(runtime.player_instance.stop_playback)
            playback_state.current_track_info = None
            playback_state.last_track_info = None
        playback_queue.queue.reset()
        runtime.music_library.scanner = _library_scanner_for(root, library_id)
        runtime.music_library.scanner.prepare_scan_status()
        runtime.library_scan_task = _create_library_refresh_task(runtime.music_library.scanner, name="selected-library-scan")
        return manager.status_cached()


@app.post("/api/library/refresh")
async def refresh_library():
    scanner = runtime.music_library.scanner
    if scanner:
        if not scanner.scanning:
            scanner.prepare_scan_status()
            _create_library_refresh_task(
                scanner,
                name="manual-library-refresh",
            )
        return {"status": "scanning", **scanner.status()}
    return {"status": "error", "message": "Library scanner not initialized"}

@app.post("/api/download")
async def start_download(request: Request):
    global downloader
    active_downloader = downloader
    if not active_downloader:
        raise HTTPException(status_code=503, detail="Downloader not available")
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    url = body.get("url")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(status_code=400, detail="URL is required")
    try:
        # Validation resolves DNS and must not block the FastAPI event loop.
        await asyncio.to_thread(active_downloader.download, url)
        # No fabricated name up front: the real saved filename is only known
        # once yt-dlp reports it, and /api/download/status serves it as it
        # becomes available (active_download["filename"]).
        return {"status": "started", "filename": None}
    except HTTPException:
        raise
    except BlockedUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise internal_error("Download start failed", exc)

@app.post("/api/download/cancel")
async def cancel_download():
    global downloader
    if not downloader:
        raise HTTPException(status_code=503, detail="Downloader not available")
    downloader.cancel()
    return {"status": "cancelled"}

@app.get("/api/download/status")
async def download_status():
    global downloader
    if downloader is None:
        raise HTTPException(status_code=503, detail="Downloader not available")
    if downloader.active_download:
        return downloader.active_download
    return {"status": "idle"}


import streaming.api as streaming_api


def _make_streaming_api_deps() -> streaming_api.StreamingApiDeps:
    """Bind the streaming provider API to the application services.

    All entries resolve the current runtime state at call time.
    """
    return streaming_api.StreamingApiDeps(
        get_qobuz_ui_state=lambda *a, **k: get_qobuz_ui_state(*a, **k),
        is_qobuz_playback_active=lambda state: _is_qobuz_playback_active(state),
        qobuz_pause=lambda: qobuz_pause(),
        broadcast_qobuz_state=lambda *a, **k: broadcast_qobuz_state(*a, **k),
        qobuz_target_track_from_state=lambda state: _qobuz_target_track_from_state(state),
        qobuz_target_rate=lambda state: _qobuz_target_rate(state),
        qobuz_pin_unity=lambda: _qobuz_pin_unity(),
        qobuz_volume_action=lambda percent: _qobuz_volume_action(percent),
        spotify_volume_action=lambda percent: _spotify_volume_action(percent),
        publish_committed_playback_owner=lambda owner, tid: _publish_committed_playback_owner(owner, tid),
        transition_error_http=lambda exc: _transition_error_http(exc),
        request_origin_is_trusted=lambda req: is_request_origin_trusted(req),
        coordinator_rate_change=lambda rate: _coordinator_rate_change(rate),
        run_coordinated_transition=lambda req: _run_coordinated_transition(req),
        get_current_playback_owner=lambda: playback_state.current_playback_owner,
        spotify_playerctl_watch=spotify_playerctl_watch,
        api_spotify_play=lambda: api_spotify_play(),
        api_spotify_toggle=lambda: api_spotify_toggle(),
        capture_source_intent=lambda: playback_state.capture_source_intent(),
        ensure_source_intent_current=lambda captured: _ensure_source_intent_current(captured),
    )


# Register streaming/provider-admin routes (real implementation in streaming/api.py).
streaming_api.register_streaming_routes(app, _make_streaming_api_deps())


@app.get("/api/system/device-name")
async def api_get_device_name():
    """Current LAN device name (*.local) with change capability info."""
    return {
        "hostname": streaming_api._mdns_device_name(),
        "can_change": shutil.which("hostnamectl") is not None,
    }


@app.post("/api/system/device-name")
async def api_set_device_name(request: Request):
    """Change the LAN device name via the existing hostnamectl mechanism.

    Reuses the exact hostname rule the installer applies: lowercase, digits and
    hyphens, no leading/trailing hyphen, plus the reserved ``localhost``.
    Avahi is restarted (when present) so the new name is advertised immediately.
    """
    if not is_request_origin_trusted(request):
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    body = await _json_object(request)
    if shutil.which("hostnamectl") is None:
        raise HTTPException(status_code=503, detail="hostnamectl is not available on this system")
    value = str(body.get("hostname") or "").strip().strip(".")
    if not _LOCAL_HOSTNAME_PATTERN.fullmatch(value) or value in _LOCAL_HOSTNAME_RESERVED:
        raise HTTPException(status_code=400, detail="Use only lowercase letters, digits and hyphens (no leading/trailing hyphen)")
    current = streaming_api._mdns_device_name()
    if value == current:
        return {"hostname": current, "changed": False}
    proc = await asyncio.create_subprocess_exec(
        "hostnamectl", "--no-ask-password", "set-hostname", value,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    communicate_task = asyncio.create_task(proc.communicate())
    try:
        _stdout, stderr = await asyncio.wait_for(asyncio.shield(communicate_task), timeout=15)
    except asyncio.TimeoutError:
        if await pw_link.stop_command_child_cancellation_safe(
            proc, _SERVICE_RESTART_TERMINATE_GRACE_SECONDS
        ):
            raise asyncio.CancelledError
        try:
            _stdout, stderr = communicate_task.result()
        except Exception:
            _stdout, stderr = b"", b""
        raise HTTPException(status_code=504, detail="hostnamectl timed out") from None
    except asyncio.CancelledError:
        await pw_link.stop_command_child_cancellation_safe(
            proc, _SERVICE_RESTART_TERMINATE_GRACE_SECONDS
        )
        raise
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip() or "hostnamectl set-hostname failed"
        if "interactive authentication" in detail:
            detail += " (device-name polkit rule missing or outdated; rerun install.sh)"
            logger.warning("hostnamectl refused without polkit auth: %s", detail)
            raise HTTPException(status_code=403, detail=detail)
        raise HTTPException(status_code=500, detail=detail)
    avahi = await asyncio.create_subprocess_exec(
        "systemctl", "restart", "avahi-daemon.service",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(asyncio.shield(avahi.wait()), timeout=10)
    except asyncio.TimeoutError:
        if await pw_link.stop_command_child_cancellation_safe(
            avahi, _SERVICE_RESTART_TERMINATE_GRACE_SECONDS
        ):
            raise asyncio.CancelledError
    except asyncio.CancelledError:
        await pw_link.stop_command_child_cancellation_safe(
            avahi, _SERVICE_RESTART_TERMINATE_GRACE_SECONDS
        )
        raise
    response: dict = {"hostname": value, "changed": True}
    try:
        sync_result = await streaming_api._sync_spotify_connect_name_best_effort()
        if sync_result.get("desired"):
            response["spotify_connect_name"] = sync_result.get("desired")
            response["spotify_device_updated"] = bool(sync_result.get("changed"))
    except Exception as exc:
        logger.warning("Spotify Connect name sync after hostname change failed: %s", exc)
    return response


# ---------------------------------------------------------------------------
# Spotify (playerctl / MPRIS)
# ---------------------------------------------------------------------------

@app.get("/api/spotify/status")
async def api_spotify_status():
    data = await get_spotify_ui_state()
    playback_state.latest_spotify_state = data
    await peak_monitor_coordinator.sync_spotify_state(data)
    return data


def _spotify_producer_for_coordinator(relax_to_any: bool = False) -> tuple[str, str] | None:
    """Resolve the concrete Spotify producer ports from the live sink input.

    Desktop and spotifyd are two renderers of the same logical ``spotify``
    source.  The concrete producer is the active sink input's PipeWire node:
    its ports are normally ``<node.name>:output_FL/FR``.  The spotifyd Pulse
    backend can expose an empty ``node.name``; PipeWire then reports its ports
    as ``:output_FL/FR``.  That anonymous form is accepted only after the
    sink input was identified as spotifyd, never as a generic fallback.
    """
    entries = media_readiness.list_spotify_sink_inputs()
    if not entries:
        return None
    obs = media_readiness.spotify_sink_input_observation(entries)
    if obs is None and not relax_to_any:
        return None
    candidate = None
    if obs is not None:
        identity, _rate = obs
        for entry in entries:
            props = entry.get("properties") or {}
            cand = entry.get("id")
            if cand is None:
                cand = (
                    props.get("node.name"),
                    props.get("application.name") or props.get("application.id"),
                    props.get("media.name"),
                )
            if cand == identity:
                candidate = entry
                break
    elif relax_to_any and entries:
        candidate = entries[0]
    if candidate is None:
        return None
    props = candidate.get("properties") or {}
    process_binary = str(props.get("application.process.binary") or "").strip().lower()
    process_binary = process_binary.rsplit("/", 1)[-1]
    node_name = str(props.get("node.name") or "").strip()
    if not node_name and (process_binary == "spotifyd" or process_binary.startswith("spotifyd.")):
        return (":output_FL", ":output_FR")
    if not node_name:
        node_name = str(
            props.get("application.name") or props.get("application.id") or ""
        ).strip()
    if not node_name:
        return None
    return (f"{node_name}:output_FL", f"{node_name}:output_FR")


def _resolve_playback_source_producer_ports(source: str | None) -> tuple[str, str] | None:
    """Resolve the producer ports for one source at verification time."""
    if source == "spotify":
        return _spotify_producer_for_coordinator()
    return source_policy.graph_port_names(source)


@app.post("/api/spotify/play")
async def api_spotify_play():
    source_intent = _capture_source_intent()
    spotifyd_volume_watch.reset_session()
    target_rate = _coordinator_target_rate("spotify")
    rate_change = await asyncio.to_thread(_coordinator_rate_change, target_rate)
    request = TransitionRequest(
        operation="spotify-play",
        source="spotify",
        target_rate=target_rate,
        should_play=True,
        rate_change=rate_change,
        reload_source=True,
        detail="api-spotify-play",
    )
    try:
        result = await _run_coordinated_transition(request)
    except ValueError as exc:
        raise bad_request(exc) from exc
    except PlaybackTransitionFailure as exc:
        raise _transition_error_http(exc) from exc
    # After the coordinator commit the Spotify source is already the
    # committed playback context; the subsequent state read is
    # telemetry/UI refresh and not part of the ownership boundary.
    # Publish the authoritative owner synchronously before any further await
    # so the ended waiter never sees a window with a new token and an old
    # footer, and the browser resolves the owner on the playback channel.
    if not getattr(result, "committed", False):
        _raise_for_uncommitted_transition(result, what="spotify playback")
    _ensure_source_intent_current(source_intent)
    await _publish_committed_playback_owner("spotify", getattr(result, "transition_id", None))
    playback_state.latest_spotify_state = await get_spotify_ui_state()
    return await broadcast_spotify_state(playback_state.latest_spotify_state)


@app.post("/api/spotify/pause")
async def api_spotify_pause():
    # Spotify pause is an MPRIS transport command, not a source handoff.
    data = await spotify_pause()
    return await broadcast_spotify_state(data)


@app.post("/api/spotify/toggle")
async def api_spotify_toggle():
    source_intent = _capture_source_intent()
    spotifyd_volume_watch.reset_session()
    sd = await get_spotify_ui_state()
    if sd.get("status") == "Playing":
        # Toggling an already-playing Spotify source is transport-only.  In
        # particular it must not quiet MPV or clear the local queue/context.
        data = await spotify_pause()
        return await broadcast_spotify_state(data)

    target_rate = _coordinator_target_rate("spotify")
    rate_change = await asyncio.to_thread(_coordinator_rate_change, target_rate)
    request = TransitionRequest(
        operation="spotify-toggle",
        source="spotify",
        target_rate=target_rate,
        should_play=True,
        rate_change=rate_change,
        reload_source=True,
        detail="api-spotify-toggle",
    )
    try:
        result = await _run_coordinated_transition(request)
    except ValueError as exc:
        raise bad_request(exc) from exc
    except PlaybackTransitionFailure as exc:
        raise _transition_error_http(exc) from exc
    # Same ownership contract as api_spotify_play: the authoritative owner
    # and token are published synchronously after the commit, before the
    # Spotify state is read or broadcast.
    if not getattr(result, "committed", False):
        _raise_for_uncommitted_transition(result, what="spotify playback")
    _ensure_source_intent_current(source_intent)
    await _publish_committed_playback_owner("spotify", getattr(result, "transition_id", None))
    data = await get_spotify_ui_state()
    return await broadcast_spotify_state(data)


@app.post("/api/spotify/next")
async def api_spotify_next():
    # Next/previous stay within Spotify and never affect the FXRoute source.
    data = await spotify_next()
    return await broadcast_spotify_state(data)


@app.post("/api/spotify/previous")
async def api_spotify_previous():
    data = await spotify_previous()
    return await broadcast_spotify_state(data)


@app.post("/api/spotify/shuffle")
async def api_spotify_shuffle():
    before = await get_spotify_ui_state()
    data = await spotify_shuffle_toggle()
    data["shuffle_changed"] = before.get("shuffle") != data.get("shuffle")
    return await broadcast_spotify_state(data)


@app.post("/api/spotify/loop")
async def api_spotify_loop():
    before = await get_spotify_ui_state()
    data = await spotify_loop_cycle()
    data["loop_changed"] = before.get("loop") != data.get("loop")
    return await broadcast_spotify_state(data)


@app.post("/api/spotify/seek")
async def api_spotify_seek(request: Request):
    body = await _json_object(request)
    try:
        position = float(body.get("position", 0))
    except (ValueError, TypeError) as exc:
        raise bad_request(exc) from exc
    data = await spotify_seek_to(position)
    return await broadcast_spotify_state(data)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    # Init is queued before the client is marked ready, so it is always the
    # first payload delivered by the per-client send worker.
    await manager.send_to_client(websocket, json.dumps({"type": "init", "data": {"player": {"state": build_playback_payload()}, "spotify": await get_spotify_ui_state(), "qobuz": await get_qobuz_ui_state()}}))
    manager.mark_ready(websocket)
    disconnect_reason = "peer-closed"
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                disconnect_reason = (
                    f"peer-close-code:{message.get('code')}"
                    if message.get("code") is not None
                    else "peer-closed"
                )
                break
            text = message.get("text")
            if text is not None:
                await manager.send_to_client(websocket, json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        disconnect_reason = "peer-disconnected"
    except Exception as e:
        disconnect_reason = f"receive-error:{e}"
        logger.warning(f"WebSocket error: {e}")
    finally:
        await manager.disconnect(websocket, reason=disconnect_reason)

@app.exception_handler(MPVNotInstalledError)
async def mpv_not_installed_handler(request: Request, exc: MPVNotInstalledError):
    return JSONResponse(
        status_code=500,
        content={
            "error": "mpv is not installed",
            "message": "Please install mpv on the system: sudo apt install mpv",
        },
    )

def run_server():
    uvicorn_log_level = "debug" if str(settings.LOG_LEVEL).strip().lower() == "verbose" else settings.LOG_LEVEL.lower()
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, log_level=uvicorn_log_level, reload=False)


dsp_api.register_dsp_routes(app, _make_dsp_api_deps())
preset_loading.configure_preset_loading(_make_preset_load_deps())
canonical_volume.configure_canonical_volume(_make_canonical_volume_deps())
power_api.register_power_routes(app, power_api.PowerApiDeps(
    is_origin_trusted=is_request_origin_trusted,
))
dsp_orchestrator = DspOrchestrator(_make_dsp_orchestration_deps())

if __name__ == "__main__":
    settings = get_settings()
    run_server()
