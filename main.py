# SPDX-License-Identifier: AGPL-3.0-only

"""Main FastAPI application for FXRoute."""

import copy
import json
import logging
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
from typing import Any, Callable, List, Mapping, Optional
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
from library.sources import MusicLibraryManager
from radio.metadata import RadioMetadataService

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
COVER_CACHE_DIR = BASE_DIR / "media" / "cache" / "covers"
TOP40_COVER_IMAGE = STATIC_DIR / "Top40.png"
UPDATE_SCRIPT = BASE_DIR / "scripts" / "update_fxroute.sh"
# Same rule as install.sh valid_local_hostname().
_LOCAL_HOSTNAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_LOCAL_HOSTNAME_RESERVED = {"localhost"}

# Cooldown to prevent rapid mpv IPC flooding (ms)
PLAY_COMMAND_COOLDOWN_MS = 400
LOCAL_TRACK_SWITCH_SETTLE_MS = 260
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


async def _run_update_script(timeout: float, *args: str) -> dict:
    """Run scripts/update_fxroute.sh in its own process group, bounded.

    The script and every git/pip/npm child it spawns live in a dedicated
    session (start_new_session=True), so the whole child tree can be
    signalled as a group via killpg(proc.pid).  stdout/stderr are drained
    by exactly one communicate() task; on timeout the group is
    TERM->grace->KILLed and that same task is drained terminally.  Caller
    cancellation runs the identical cleanup and re-raises CancelledError
    afterwards.  A timeout is reported through the existing result shape
    (returncode -1 plus a stderr note), never as a new exception.
    """
    if not UPDATE_SCRIPT.exists():
        raise HTTPException(status_code=500, detail=f"Update script missing: {UPDATE_SCRIPT}")
    proc = await asyncio.create_subprocess_exec(
        str(UPDATE_SCRIPT),
        *args,
        cwd=str(BASE_DIR),
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    communicate_task = asyncio.create_task(proc.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(
            asyncio.shield(communicate_task), timeout=timeout
        )
    except asyncio.TimeoutError:
        if await pw_link.stop_process_group_cancellation_safe(
            proc, communicate_task, grace_seconds=_UPDATE_TERMINATE_GRACE_SECONDS
        ):
            raise asyncio.CancelledError
        try:
            stdout, stderr = communicate_task.result()
        except Exception:
            stdout, stderr = b"", b""
        return {
            "returncode": -1,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace")
            + f"\nUpdate command timed out after {int(timeout)} seconds",
        }
    except asyncio.CancelledError:
        await pw_link.stop_process_group_cancellation_safe(
            proc, communicate_task, grace_seconds=_UPDATE_TERMINATE_GRACE_SECONDS
        )
        raise
    return {
        "returncode": proc.returncode,
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
    }


_update_operation_lock: Optional[asyncio.Lock] = None


def _get_update_operation_lock() -> asyncio.Lock:
    global _update_operation_lock
    if _update_operation_lock is None:
        _update_operation_lock = asyncio.Lock()
    return _update_operation_lock


async def _run_update_operation(timeout: float, *args: str) -> dict:
    """Run an update-script invocation under the exclusive update guard.

    Only one update/check/restore may use update_fxroute.sh at a time; a
    second operation is rejected immediately with HTTP 409 instead of
    silently waiting behind the first one.  The guard is released in a
    finally, so success, timeout, cancellation and ordinary exceptions all
    free it again.
    """
    lock = _get_update_operation_lock()
    if lock.locked():
        raise HTTPException(
            status_code=409, detail="An update operation is already in progress"
        )
    async with lock:
        return await _run_update_script(timeout, *args)


async def _restart_fxroute_service_after_response(service_name: str) -> None:
    """Hand the FXRoute service restart to systemd, bounded.

    The restart job itself is executed by systemd --user; this process only
    enqueues it (--no-block) and must not wait for its own stop+start,
    because the response was already sent and this process is expected to be
    stopped by systemd as part of the job.  The systemctl client is bounded:
    a timeout means "restart outcome unknown" and is only logged; the
    already sent update/restore response stays unchanged.  Cancellation runs
    the same shielded terminal cleanup (no orphan even under a second
    cancellation) and re-raises CancelledError afterwards.  A nonzero
    systemctl exit already proves the job enqueue failed and is logged.
    """
    await asyncio.sleep(0.8)
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "--no-block",
            "restart",
            f"{service_name}.service",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.warning("Deferred FXRoute service restart failed: %s", exc)
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_SERVICE_RESTART_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        if await pw_link.stop_command_child_cancellation_safe(
            proc, _SERVICE_RESTART_TERMINATE_GRACE_SECONDS
        ):
            raise asyncio.CancelledError
        logger.warning(
            "Deferred FXRoute service restart timed out after %s s; restart outcome unknown",
            _SERVICE_RESTART_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        await pw_link.stop_command_child_cancellation_safe(
            proc, _SERVICE_RESTART_TERMINATE_GRACE_SECONDS
        )
        raise
    else:
        if proc.returncode != 0:
            logger.warning(
                "Deferred FXRoute service restart exited with code %s",
                proc.returncode,
            )


def _measurement_blocks_playback_rate(expected_rate: Optional[int]) -> Optional[int]:
    """Resolve the session-owned playback-rate block decision for samplerate deps.

    The active-and-jobs decision lives on ``MeasurementSampleRateSession``
    (``blocks_playback_rate``); this is only the injection bridge that guards
    the not-yet-created session and delegates to the owner.
    """
    if measurement_sr_session is None:
        return None
    return measurement_sr_session.blocks_playback_rate(expected_rate)


def _is_local_playback_active(state: dict | None) -> bool:
    return playback_state_helpers.is_local_playback_active(state)

def _is_spotify_playback_active(state: dict | None) -> bool:
    return playback_state_helpers.is_spotify_playback_active(state)


def _is_measurement_window_open() -> bool:
    if last_measurement_window_seen_at <= 0:
        return False
    return (time.monotonic() - last_measurement_window_seen_at) <= MEASUREMENT_WINDOW_TTL_SECONDS


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
    if measurement_window_open:
        reason = "measurement_window"
    elif playback_active:
        reason = "playback"
    else:
        reason = "idle"
    return {
        "amp_should_be_on": bool(playback_active or measurement_window_open),
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
from dsp.manager import DSPManager
from dsp.runtime import DSPRuntime, DSPRuntimeConfig, _contains_link
import dsp.api as dsp_api
import dsp.orchestration as dsp_orchestration
import playback.orchestration as playback_orchestration
from audio import pw_link
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
    OUTPUT_MODE_SUBWOOFER_21,
    OUTPUT_MODE_SUBWOOFER_22,
    OUTPUT_MODE_SUBWOOFER_22_STEREO,
    OUTPUT_MODE_SUBWOOFER_22_MODES,
    OUTPUT_MODE_SUBWOOFER_MODES,
    SOURCE_MODE_APP_PLAYBACK,
    SOURCE_MODE_BLUETOOTH_INPUT,
    SOURCE_MODE_EXTERNAL_INPUT,
    apply_persisted_audio_output_selection,
    disconnect_connected_bluetooth_audio_sources,
    get_audio_output_overview,
    get_audio_source_overview,
    get_bluetooth_audio_overview,
    get_samplerate_status,
    normalize_sample_rate_policy,
    persist_audio_output_mode,
    prepare_audio_output_mode,
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
import audio.power as system_power
import measurement.spl_calibration as spl_calibration
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
        auto_sub_active=lambda: autosub.is_optimization_active(),
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
    )


measurement_session.configure_services(_make_measurement_services())
autosub.configure_dependencies(autosub.AutoSubDependencies(
    get_dsp_runtime=lambda: runtime.dsp_runtime,
    get_measurement_store=lambda: measurement_store,
    get_measurement_session=lambda: measurement_sr_session,
    get_dsp_manager=lambda: dsp_manager,
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
        persist_audio_output_mode=lambda *a, **k: persist_audio_output_mode(*a, **k),
        trigger_idle_sink_renegotiation=lambda *a, **k: samplerate.trigger_idle_sink_renegotiation(*a, **k),
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
class _ClientSender:
    """Bounded per-client delivery queue with exactly one send worker.

    One worker per client serializes all sends (FIFO by construction) and
    every send is bounded by a timeout.  When the bounded queue is full the
    caller treats the client as too slow; the worker failing (timeout, send
    error, vanished socket) marks the client for removal.
    """

    def __init__(
        self,
        websocket: WebSocket,
        *,
        timeout: float,
        max_pending: int,
    ) -> None:
        self.websocket = websocket
        self.timeout = timeout
        self.queue: "asyncio.Queue[tuple[str | None, str | None]]" = asyncio.Queue(maxsize=max_pending)
        self._coalesced: dict[str, str] = {}
        self.ready = False
        self.failed = False
        self.failure_reason = "send-worker-failed"
        self._task: "asyncio.Task | None" = None

    def enqueue(self, data: str, *, coalesce_key: str | None = None) -> bool:
        """Queue one payload; False when the bounded queue is full."""
        if coalesce_key is not None and coalesce_key in self._coalesced:
            self._coalesced[coalesce_key] = data
            return True
        try:
            self.queue.put_nowait((coalesce_key, None if coalesce_key else data))
        except asyncio.QueueFull:
            return False
        if coalesce_key is not None:
            self._coalesced[coalesce_key] = data
        return True

    async def close(self) -> None:
        """Cancel the worker and drain any queued payloads."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.queue.task_done()
        self._coalesced.clear()

    async def run(self) -> None:
        while True:
            coalesce_key, data = await self.queue.get()
            try:
                if coalesce_key is not None:
                    data = self._coalesced.pop(coalesce_key)
                if data is None:
                    continue
                if self.websocket.client_state.name != "CONNECTED":
                    raise RuntimeError("websocket is no longer CONNECTED")
                await asyncio.wait_for(
                    self.websocket.send_text(data), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                self.failed = True
                self.failure_reason = f"send-timeout:{self.timeout:.1f}s"
                return
            except Exception as exc:
                self.failed = True
                self.failure_reason = f"send-error:{exc}"
                return
            finally:
                self.queue.task_done()


class ConnectionManager:
    """Fan-out broadcasts without letting one slow client stall the others.

    Every connected client owns one bounded delivery queue and exactly one
    send worker; all sends for a socket (broadcasts, init, pong) go through
    that worker, so per-client ordering is FIFO and concurrent ``send_text``
    calls are impossible.  A stuck client only delays its own queue; a
    timeout or send error removes the client. Repeated state snapshots share
    one pending slot per type; a full queue of distinct events disconnects the
    overloaded client instead of silently dropping events.
    """

    def __init__(self, send_timeout: float = 5.0, max_pending_sends: int = 8):
        self.active_connections: List[WebSocket] = []
        self._list_lock = asyncio.Lock()
        self._senders: dict[WebSocket, _ClientSender] = {}
        self._worker_tasks: set[asyncio.Task] = set()
        self._send_timeout = max(0.05, send_timeout)
        self._max_pending_sends = max(1, max_pending_sends)
        self._coalesced_message_types = {
            "playback",
            "spotify",
            "dsp",
            "playback_peak_warning",
        }

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        sender = _ClientSender(
            websocket,
            timeout=self._send_timeout,
            max_pending=self._max_pending_sends,
        )
        async with self._list_lock:
            self.active_connections.append(websocket)
            self._senders[websocket] = sender
        self._spawn_worker(websocket, sender)
        logger.info(f"WebSocket connected: {len(self.active_connections)} active")

    async def _unregister(self, websocket: WebSocket) -> _ClientSender | None:
        async with self._list_lock:
            sender = self._senders.pop(websocket, None)
            if sender is not None:
                try:
                    self.active_connections.remove(websocket)
                except ValueError:
                    pass
        return sender

    async def disconnect(self, websocket: WebSocket, *, reason: str = "unspecified") -> bool:
        """Remove a client from the manager (idempotent).

        The WebSocket transport close runs in an owned, bounded background
        task so a slow or stuck close can never block producers or the
        broadcast hotpath.  Normal peer disconnects are unaffected: the close
        on an already-closed transport is a no-op.
        """
        sender = await self._unregister(websocket)
        if sender is None:
            return False
        await sender.close()
        self._schedule_transport_close(websocket)
        logger.info(
            "WebSocket disconnected: reason=%s active=%s",
            reason,
            len(self.active_connections),
        )
        return True

    def _schedule_transport_close(self, websocket: WebSocket) -> None:
        """Close the transport in a bounded owned task, never awaited inline."""

        async def close_transport() -> None:
            try:
                await asyncio.wait_for(
                    websocket.close(), timeout=self._send_timeout
                )
            except asyncio.TimeoutError:
                logger.debug(
                    f"WebSocket close timed out after {self._send_timeout:.1f}s"
                )
            except Exception as exc:
                logger.debug(f"WebSocket close failed: {exc}")

        task = asyncio.create_task(close_transport(), name="ws-client-close")
        self._worker_tasks.add(task)
        task.add_done_callback(self._worker_tasks.discard)

    async def send_to_client(
        self,
        websocket: WebSocket,
        data: str,
        *,
        coalesce_key: str | None = None,
    ) -> bool:
        """Queue one payload for a client's send worker.

        Returns False when the client is gone, its worker already failed, or
        its bounded queue is full; the full-queue case disconnects the client
        as too slow.
        """
        sender = self._senders.get(websocket)
        if sender is None or sender.failed:
            return False
        if not sender.enqueue(data, coalesce_key=coalesce_key):
            await self.disconnect(websocket, reason="send-queue-full")
            return False
        return True

    def mark_ready(self, websocket: WebSocket) -> bool:
        """Unlock normal broadcasts for a client whose init is queued first."""
        sender = self._senders.get(websocket)
        if sender is None:
            return False
        sender.ready = True
        return True

    def _spawn_worker(self, websocket: WebSocket, sender: _ClientSender) -> None:
        task = asyncio.create_task(sender.run(), name="ws-client-sender")
        sender._task = task
        self._worker_tasks.add(task)

        def finished(_task: asyncio.Task) -> None:
            self._worker_tasks.discard(_task)
            if _task.cancelled() or not sender.failed:
                return
            cleanup = asyncio.create_task(
                self.disconnect(
                    websocket,
                    reason=getattr(sender, "failure_reason", "send-worker-failed"),
                ),
                name="ws-client-failure-cleanup",
            )
            self._worker_tasks.add(cleanup)
            cleanup.add_done_callback(self._worker_tasks.discard)

        task.add_done_callback(finished)

    async def broadcast(self, message: dict) -> None:
        data = json.dumps(message)
        message_type = message.get("type")
        coalesce_key = (
            str(message_type)
            if message_type in self._coalesced_message_types
            else None
        )
        async with self._list_lock:
            connections = list(self.active_connections)
        for connection in connections:
            if connection.client_state.name != "CONNECTED":
                await self.disconnect(connection, reason="transport-not-connected")
                continue
            sender = self._senders.get(connection)
            if sender is None or not sender.ready:
                continue
            await self.send_to_client(
                connection,
                data,
                coalesce_key=coalesce_key,
            )

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
    dsp_out_3 = "fxroute_dsp:output_3"
    dsp_out_4 = "fxroute_dsp:output_4"
    hw_fl = f"{output_key}:playback_FL" if output_key else ""
    hw_fr = f"{output_key}:playback_FR" if output_key else ""
    hw_rl = f"{output_key}:playback_RL" if output_key else ""
    hw_rr = f"{output_key}:playback_RR" if output_key else ""
    links = {
        "sink_to_dsp_left": _contains_link(link_text, sink_monitor_left, dsp_in_left),
        "sink_to_dsp_right": _contains_link(link_text, sink_monitor_right, dsp_in_right),
        "dsp_main_left_to_hw": bool(hw_fl) and _contains_link(link_text, dsp_out_1, hw_fl),
        "dsp_main_right_to_hw": bool(hw_fr) and _contains_link(link_text, dsp_out_2, hw_fr),
        "dsp_sub_left_to_hw": bool(hw_rl) and _contains_link(link_text, dsp_out_3, hw_rl),
        "dsp_sub_right_to_hw": bool(hw_rr) and _contains_link(link_text, dsp_out_4, hw_rr),
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


def ensure_local_source_volume() -> None:
    if not runtime.player_instance or not runtime.player_instance._running:
        return
    try:
        runtime.player_instance.set_volume(100)
    except Exception as exc:
        logger.warning("Failed to pin MPV source volume to 100%%: %s", exc)


def get_output_volume_safe(default: int = 100) -> int:
    # The global FXRoute master is the single user-facing volume.  Loudness
    # volumeDb is only the ISO-226 work point and must never be reported as
    # the volume.
    return get_status_volume(default)


async def _volume_state_for_manager(
    manager, *, live_master: int | None = None
) -> volume_contract.VolumeState:
    extras = {}
    preset = ""
    if manager:
        load_extras = getattr(manager, "load_global_extras", None)
        if callable(load_extras):
            extras = load_extras() or {}
        get_active = getattr(manager, "get_active_preset", None)
        if callable(get_active):
            preset = get_active() or ""
    loudness = extras.get("loudness") if isinstance(extras, dict) else {}
    loudness = loudness if isinstance(loudness, dict) else {}
    params = loudness.get("params") if isinstance(loudness.get("params"), dict) else {}
    enabled = bool(loudness.get("enabled"))
    if live_master is None:
        live_master = int(await _drain_worker(get_output_volume))
    guard = 0.0
    if runtime.dsp_runtime is not None:
        try:
            guard = float(runtime.dsp_runtime.snapshot().get("output_gain_db") or 0.0)
        except Exception:
            guard = 0.0
    return volume_contract.VolumeState(
        preset=preset,
        loudness_enabled=enabled,
        volume_db=float(params.get("volumeDb") or 0.0),
        master_percent=int(live_master),
        dsp_guard_db=guard,
    )


async def _set_canonical_output_volume(volume: float | int) -> dict[str, Any]:
    """Apply the one global FXRoute master volume for every source.

    The footer slider (and the remote Connect bridges) drives only the master.
    Loudness volumeDb is the ISO-226 work point and is never touched here.
    Writes are serialized against concurrent canonical volume writes.
    """
    async with _canonical_volume_write_lock():
        requested = max(0, min(100, int(round(float(volume)))))
        verified = await _drain_worker(set_output_volume, requested)
        return {"volume": int(verified)}


async def _apply_remote_volume_value(
    volume_percent: int,
    *,
    owner: str | None = None,
    source_active: Callable[[], bool] | None = None,
) -> None:
    """Apply an absolute remote Connect volume to the canonical master.

    Called by the Connect bridges only after pickup (the gesture crossed the
    current master level), so the write adopts the controller value without
    any jump larger than the gesture step. The owner check is repeated while
    holding the canonical write lock, and an owner transition during the
    non-cancellable worker call restores the pre-write master.
    """
    owner_is_current = lambda: (
        (owner is None or _resolve_playback_owner() == owner)
        and (source_active is None or source_active())
    )
    if not owner_is_current():
        return
    async with _canonical_volume_write_lock():
        if not owner_is_current():
            return
        current = get_output_volume_safe()
        requested = max(0, min(100, int(round(float(volume_percent)))))
        try:
            await _drain_worker(set_output_volume, requested)
        finally:
            if owner is not None and not owner_is_current():
                try:
                    await _drain_worker(set_output_volume, current)
                except Exception as exc:
                    logger.warning("Failed to restore master after remote owner loss: %s", exc)


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

    await runtime.dsp_runtime.guarded_rebuild(
        overview,
        guard_db=guard_db,
        apply_candidate=persist_candidate,
        apply_previous=lambda: dsp_manager.save_global_extras(previous),
        settle_seconds=settle,
        candidate_extras=candidate,
        previous_extras=previous,
    )
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
    if playback_state.current_playback_owner == "qobuz":
        return await get_qobuz_ui_state()
    qobuz_state = await get_qobuz_ui_state()
    if not _is_qobuz_playback_active(qobuz_state):
        return qobuz_state

    async def skip_if_owner_committed() -> bool:
        # Same re-validation contract as the Spotify claim: the guard above
        # runs before the transition lock, so a claim queued behind an
        # FXRoute-initiated Qobuz start must be re-checked inside the lock.
        return (
            playback_state.current_playback_owner == "qobuz"
            or playback_state.playback_intent_generation != claim_intent_generation
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
        return (
            playback_state.current_playback_owner == "spotify"
            or playback_state.playback_intent_generation != claim_intent_generation
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
    except Exception:
        pass
    try:
        await broadcast_spotify_state()
    except Exception:
        pass


async def pause_local_playback_for_spotify_broadcast():
    try:
        if runtime.player_instance and runtime.player_instance._running:
            await _drain_worker(runtime.player_instance.stop_playback)
            playback_state.current_track_info = None
            await manager.broadcast({"type": "playback", "data": build_playback_payload(runtime.player_instance.state)})
            released = await media_readiness.wait_for_pipewire_mpv_release()
            if not released:
                await asyncio.sleep(SOURCE_HANDOFF_SETTLE_MS / 1000)
    except Exception:
        pass


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

        downloader = Downloader()
        logger.info("Downloader initialized")

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
        runtime_loop = asyncio.get_running_loop()

        def guarded_effects_transition(previous, candidate, persist_all_presets):
            return asyncio.run_coroutine_threadsafe(
                _guarded_effects_transition(previous, candidate, persist_all_presets),
                runtime_loop,
            ).result()

        dsp_manager.runtime_transition_callback = guarded_effects_transition

        def temporary_effects_transition(previous, candidate):
            async def transition():
                async with _dsp_mutation_lock():
                    await runtime.dsp_runtime.guarded_rebuild(
                        get_audio_output_overview(),
                        guard_db=-18.0,
                        apply_candidate=lambda: None,
                        apply_previous=lambda: None,
                        settle_seconds=dsp_manager.LOUDNESS_STRENGTH_VOLUME_SETTLE_SECONDS,
                        candidate_extras=candidate,
                        previous_extras=previous,
                    )
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
        except Exception as exc:
            logger.warning("Failed to re-apply source monitoring: %s", exc)

        bluetooth_input.monitor_task = asyncio.create_task(
            bluetooth_input.run_monitor_loop(),
            name="bluetooth-input-monitor",
        )
        spotify_playerctl_watch.last_trigger_at = 0.0
        logger.info("Starting Spotify playerctl watch task")
        spotify_playerctl_watch.watch_task = asyncio.create_task(
            spotify_playerctl_watch.run_watch_loop(),
            name="spotify-playerctl-watch",
        )
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

    async def _disconnect_all_websockets() -> None:
        connections = list(manager.active_connections)
        for connection in connections:
            try:
                await manager.disconnect(connection, reason="server-shutdown")
            except Exception:
                logger.exception("Failed to disconnect websocket client during shutdown")
        pending = [task for task in set(manager._worker_tasks) if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    if manager.active_connections or manager._worker_tasks:
        await cleanup("websocket-clients", _disconnect_all_websockets)

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
        restore_volume_state=lambda *args, **kwargs: _restore_volume_state(*args, **kwargs),
        volume_state_for_manager=lambda *args, **kwargs: _volume_state_for_manager(*args, **kwargs),
        schedule_peak_monitor_refresh=lambda reason: dsp_orchestrator.schedule_peak_monitor_refresh_after_effects_change(reason),
    )


def _current_output_mode() -> str:
    """Cheap current output mode for the subwoofer link-watcher gate.

    Prefers the committed DSP runtime config; falls back to the persisted
    audio output mode (the same source the output-mode route uses).  It never
    builds the PipeWire overview, so the idle link-watcher tick stays cheap.
    """
    dsp_runtime = runtime.dsp_runtime
    if dsp_runtime is not None:
        snapshot = dsp_runtime.snapshot()
        mode = (snapshot.get("config") or {}).get("output_mode")
        if mode:
            return str(mode)
    return samplerate._load_audio_output_mode().get("mode") or OUTPUT_MODE_STEREO


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
    )


def _make_playback_orchestration_deps() -> playback_orchestration.PlaybackOrchestrationDeps:
    """Bind transition/recovery orchestration to live application services."""
    return playback_orchestration.PlaybackOrchestrationDeps(
        get_coordinator=lambda: playback_transition_coordinator,
        set_coordinator=lambda value: globals().__setitem__("playback_transition_coordinator", value),
        make_transition_coordinator=lambda: PlaybackTransitionCoordinator(
            FxrouteTransitionRuntime(make_playback_runtime_deps()),
            gate_state_path=_playback_gate_state_path(),
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
        Middleware(_SkipPrecompressedAssetsForGZip),
        Middleware(GZipMiddleware, minimum_size=1024),
    ],
)
app.include_router(radio_api_router)
app.include_router(spl_calibration.router)
app.include_router(library_api_router)
app.include_router(autosub.router)
app.include_router(measurement_session.router)

# Static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

def _effective_request_scheme(request: Request) -> str:
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    if forwarded_proto:
        return forwarded_proto
    return (request.url.scheme or "http").lower()


def _effective_request_host(request: Request) -> str:
    """Resolve the host the client *thinks* it is talking to.

    Honors ``X-Forwarded-Host`` when the install is behind a reverse proxy
    (FXRoute only ever trusts a single hop here, matching the project's
    trust assumptions for the optional Caddy reverse proxy on the LAN).
    The value is lower-cased and stripped of optional ``:port`` so it can
    be compared against ``Origin`` / ``Referer`` headers verbatim.
    """

    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip().lower()
    if forwarded_host:
        # Hostname only -- the matching port lives in X-Forwarded-Port.
        return forwarded_host.split(":", 1)[0]
    return (request.url.hostname or "").lower()


def _effective_request_port(request: Request) -> Optional[int]:
    """Resolve the frontend port the client believes it is talking to.

    Honors ``X-Forwarded-Port`` so the optional Caddy reverse proxy on
    the LAN is supported without losing the same-origin defence.  When no
    forward headers are set, the request's actual URL port is used.
    """

    raw = (request.headers.get("x-forwarded-port") or "").split(",", 1)[0].strip()
    if raw.isdigit():
        return int(raw)
    try:
        return request.url.port
    except ValueError:
        # Malformed Host port: return a sentinel that can never equal a
        # parsed header port (those are 0-65535 or None) and never passes
        # the default-port rules, so the origin comparison fails closed
        # instead of raising.
        return -1


def _request_origin_is_trusted(request: Request) -> bool:
    """Cross-site defence for state-changing endpoints.

    FXRoute's LAN security baseline is "trusted LAN, no auth, no cookies"
    (see ``AGENTS.md``).  Within that baseline, requests originating from
    a foreign web page in the same browser are *not* a trusted caller, so
    we cannot rely solely on the LAN assumption.

    The routine therefore refuses a POST whose ``Origin`` or ``Referer``
    header points to a different scheme + host + port than the one the
    request is actually reaching -- the cheap, no-cookie CSRF defence
    recommended when an application cannot introduce a new auth surface.
    Requests without either header (the legitimate CLI / curl / systemd
    path) are allowed so existing LAN operators do not lose their
    workflows.

    Returns ``True`` when the call is allowed, ``False`` when it must be
    rejected as a cross-site POST.
    """

    trusted_host = _effective_request_host(request)
    if not trusted_host:
        # No host to compare against: the request is malformed enough that
        # we refuse to make a decision and let the caller choose.
        return False

    trusted_scheme = _effective_request_scheme(request)
    # If the request did not run on a known port (httpx test client with
    # weird hosts) the comparison falls back to comparing host only.
    trusted_port = _effective_request_port(request)

    def _netloc_matches(parsed) -> bool:
        if not parsed.hostname:
            return False
        if parsed.hostname.lower() != trusted_host:
            return False
        try:
            header_port = parsed.port
        except ValueError:
            # Malformed header port (out of range / non-numeric): refuse the
            # request instead of letting the comparison raise a 500.
            return False
        if header_port is None and trusted_port in (None, 80, 443):
            return True
        if header_port is None:
            # Header did not include a port; fall back to comparing
            # against the request's effective default.
            if (trusted_scheme == "https" and trusted_port == 443) or (
                trusted_scheme == "http" and trusted_port in (None, 80)
            ):
                return True
            return False
        return header_port == trusted_port

    origin = (request.headers.get("origin") or "").strip().lower()
    if origin:
        if origin == "null":
            # Browsers emit Origin: null for sandboxed documents and
            # cross-origin redirects under specific referrer policies.  We
            # cannot confirm the caller's site, so refuse.
            return False
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        if parsed.scheme and parsed.scheme.lower() != trusted_scheme:
            return False
        return _netloc_matches(parsed)

    referer = (
        request.headers.get("referer")
        or request.headers.get("referrer")
        or ""
    ).strip()
    if referer:
        try:
            parsed = urlparse(referer)
        except ValueError:
            return False
        if parsed.scheme and parsed.scheme.lower() != trusted_scheme:
            return False
        return _netloc_matches(parsed)

    # No Origin AND no Referer: a CLI / systemd caller.  Allowed because
    # the LAN security baseline treats direct callers as trusted.
    return True

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    html = (STATIC_DIR / "index.html").read_text()
    if _effective_request_scheme(request) != "https":
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
    if not runtime.player_instance or not runtime.player_instance._running:
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
        if not await playback_queue.queue.load_track(target_index, transition_reason="direct queue selection"):
            raise HTTPException(status_code=409, detail="Native queue navigation failed")
        track_info = dict(playback_queue.queue.tracks[target_index])
        return {
            "status": "playing",
            "url": str(track_info.get("url") or ""),
            "track": track_info,
            "playback": build_playback_payload(runtime.player_instance.state),
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
        track_index = next(
            (index for index, item in enumerate(queue_tracks) if str(item.get("id")) == str(req.track_id)),
            -1,
        )
        queue_candidate = playback_queue.QueueCandidate(
            queue=[dict(item) for item in queue_tracks] if multi_track else [],
            original=[dict(item) for item in queue_tracks] if multi_track else [],
            index=track_index if multi_track else -1,
            mode="app_replace",
            loop=bool(req.loop),
            shuffle=bool(req.shuffle),
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
        # the committed queue state either.
        raise HTTPException(status_code=500, detail="Playback transition was not committed")
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
        body = await request.json() if request is not None else {}
        return await broadcast_spotify_state(await spotify_seek_to(float(body.get("position", 0))))
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
        body = await request.json() if request is not None else {}
        return await broadcast_qobuz_state(await provider.seek(float(body.get("position", 0))))
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
            except Exception:
                pass
        elif owner == "qobuz":
            await qobuz_pause()
            try:
                await media_readiness.wait_for_pipewire_qobuz_release()
            except Exception:
                pass
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
    return {"volume": volume_result["volume"]}

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


@app.post("/api/playback/selection")
async def sync_playback_selection(request: Request):
    if not runtime.player_instance or not runtime.player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    queue_track_ids = body.get("queue_track_ids") or []
    if not isinstance(queue_track_ids, list):
        raise HTTPException(status_code=400, detail="Invalid JSON, expected {\"queue_track_ids\": <list>}")

    scanner = runtime.music_library.scanner
    tracks = await _drain_worker(scanner.get_tracks) if scanner is not None else None
    playback = playback_queue.queue.sync_active_local_queue_selection(
        queue_track_ids=queue_track_ids,
        shuffle=bool(body.get("shuffle", False)),
        loop=bool(body.get("loop", False)),
        tracks=tracks,
    )
    await manager.broadcast({"type": "playback", "data": playback})
    return {"status": "ok", "playback": playback}


@app.post("/api/playback/shuffle")
async def set_playback_shuffle(request: Request):
    routed = await _route_global_control("shuffle", request)
    if routed is not None:
        return routed
    if not runtime.player_instance or not runtime.player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    try:
        body = await request.json()
        enabled = bool(body.get("enabled", False))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON, expected {\"enabled\": <bool>}")

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
    if not _can_send_play_command():
        state = runtime.player_instance.state
        return {"status": "ok", "position": state.get("position", 0), "playback": build_playback_payload(state)}
    try:
        body = await request.json()
        pos = float(body.get("position", 0))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON, expected {\"position\": <float>}")
    if not runtime.player_instance.state.get("current_file"):
        raise HTTPException(status_code=409, detail="Nothing loaded to seek")
    # Re-check after the last await: a transition may have started while the
    # request body was being read.  Never seek or mark intent mid-transition.
    if _playback_transition_is_active():
        raise HTTPException(status_code=409, detail="A playback transition is in progress")
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
        state["system"] = {"version": _read_version_file()}
        return state
    return {"running": False, "system": {"version": _read_version_file()}}


@app.get("/api/power/state")
async def get_power_state():
    return _build_power_state_payload()


@app.post("/api/power/measurement-heartbeat")
async def measurement_window_heartbeat(request: Request):
    global last_measurement_window_seen_at
    try:
        body = await request.json()
    except Exception:
        body = {}
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


async def _resolve_system_power_capabilities() -> system_power.PowerCapabilities:
    """Thin alias for :func:`power.get_capabilities` kept for readability.

    All capability translation, the strict-yes gate, the timeout, and the
    ``unavailable`` fall-through live in ``power.py``.  The handler here
    only routes the result into the HTTP envelope.
    """

    return await system_power.get_capabilities()


@app.get("/api/system/power")
async def system_power_capabilities():
    """Report suspend/power-off capability of systemd-logind.

    A 503 is returned only when the dbus-send binary itself is missing
    (then no capability probe can succeed at all); every other failure is
    reported through the textual ``unavailable`` value so the UI keeps
    working even on a host without a running logind.  The body shape --
    including the strict-yes ``suspend_supported`` / ``power_off_supported``
    conveniences -- and the full logind vocabulary are documented on
    :func:`power.is_logind_call_executable`.
    """

    try:
        caps = await _resolve_system_power_capabilities()
    except asyncio.TimeoutError:
        logger.warning("system power capabilities timed out")
        raise HTTPException(
            status_code=503,
            detail="systemd-logind not reachable via dbus",
        )

    return {
        "available": caps.available,
        "suspend": caps.suspend,
        "power_off": caps.power_off,
        "suspend_supported": system_power.is_logind_call_executable(caps.suspend),
        "power_off_supported": system_power.is_logind_call_executable(caps.power_off),
        "unavailable_reason": caps.unavailable_reason,
    }


def _system_power_error_to_http(result: system_power.PowerCallResult):
    """Map a failed :class:`PowerCallResult` to the right HTTP code.

    * ``"denied"``     -> 403 (polkit refused; the user-facing message is
      carried by the body).
    * ``"unavailable"`` -> 503 (dbus-send or login1 missing).
    """

    if result.status == "denied":
        return HTTPException(status_code=403, detail=result.error or "denied")
    return HTTPException(status_code=503, detail=result.error or "unavailable")


@app.post("/api/system/power/suspend")
async def system_power_suspend(request: Request):
    """Trigger ``Manager.Suspend`` via systemd-logind.

    Returns ``200 OK`` when the suspend request was dispatched
    successfully.  The HTTP response must be sent before the system
    actually suspends; the frontend uses this signal to switch the
    connection badge into the ``"Suspending…"`` state.

    Two gates run before the action:

    * :func:`_request_origin_is_trusted` rejects cross-site POSTs so a
      foreign web page in the user's browser cannot trivially shut the
      host down.  This is the standard CSRF mitigation when the
      application deliberately has no session cookies.
    * :func:`system_power.is_now_supported` re-probes ``CanSuspend`` so
      a stale UI snapshot, an inhibitor lock that engaged after the
      page loaded, or a CLI caller hitting the endpoint directly will
      all fail cleanly with HTTP 409 if logind now reports anything
      other than ``"yes"``.  This prevents FXRoute from dispatching the
      action under a different capability than the one the menu
      advertises -- which would either touch auth or inhibitor blocks
      the polkit rule does not cover, i.e. exactly the "additional
      privilege" we must not acquire.
    """

    if not _request_origin_is_trusted(request):
        raise HTTPException(
            status_code=403,
            detail="Cross-site request rejected: open FXRoute on this host before using the power menu.",
        )
    supported, raw = await system_power.is_now_supported("suspend")
    if not supported:
        raise HTTPException(
            status_code=409,
            detail=f"Suspend is not directly executable right now (logind CanSuspend={raw!r}).",
        )
    result = await system_power.request_suspend()
    if result.ok:
        return {"ok": True, "status": "suspended", "action": result.action}
    raise _system_power_error_to_http(result)


@app.post("/api/system/power/power-off")
async def system_power_power_off(request: Request):
    """Trigger ``Manager.PowerOff`` via systemd-logind.

    Returns ``200 OK`` when the shutdown request was dispatched.
    Like :func:`system_power_suspend`, this responds before logind has
    actually powered the machine off; the frontend shows
    ``"Shutting down…"`` until the websocket drops.

    The same two gates apply: cross-origin rejection through
    :func:`_request_origin_is_trusted`, plus a fresh ``CanPowerOff``
    re-probe through :func:`system_power.is_now_supported` so any
    non-``"yes"`` logind answer (``"challenge"``, ``"inhibited"`` ...)
    fails cleanly with HTTP 409 instead of bypassing the polkit rule.
    """

    if not _request_origin_is_trusted(request):
        raise HTTPException(
            status_code=403,
            detail="Cross-site request rejected: open FXRoute on this host before using the power menu.",
        )
    supported, raw = await system_power.is_now_supported("power_off")
    if not supported:
        raise HTTPException(
            status_code=409,
            detail=f"Shutdown is not directly executable right now (logind CanPowerOff={raw!r}).",
        )
    result = await system_power.request_power_off()
    if result.ok:
        return {"ok": True, "status": "shutting_down", "action": result.action}
    raise _system_power_error_to_http(result)


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
    if not _request_origin_is_trusted(request):
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    restore = "--restore" in script_args
    service_name = _configured_service_name()
    result = await _run_update_operation(_UPDATE_APPLY_TIMEOUT_SECONDS, *script_args)
    ok = result["returncode"] == 0
    stdout = result.get("stdout", "")
    if restore:
        # The restore script always reconciles the checkout; a successful
        # run therefore always needs the deferred service restart.
        update_applied = ok
    else:
        update_applied = ok and any(
            marker in stdout
            for marker in (
                "Pulling updates with fast-forward only.",
                "Checkout is current, but the deployment was not completed; retrying reconciliation.",
            )
        )
    if update_applied:
        asyncio.create_task(_restart_fxroute_service_after_response(service_name))
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
        result = set_audio_output_selection(output_key)
        await dsp_orchestrator.sync_runtime(result, reason="output-selection", retry_on_stale=True)
        result = with_subwoofer_derived_delays(result)
        if runtime.dsp_runtime is not None:
            result["output_mode"] = {
                **(result.get("output_mode") or {}),
                "runtime": runtime.dsp_runtime.snapshot(),
            }
        await dsp_orchestrator.refresh_peak_monitor_after_effects_change("audio-output-switch")
        return result
    except ValueError as exc:
        raise bad_request(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to switch audio output: {exc}")


@app.post("/api/audio/output-mode")
async def save_audio_output_mode_route(request: Request):
    try:
        body = await request.json()
        mode = str(body.get("mode", "")).strip()
        subwoofer = body.get("subwoofer") if isinstance(body.get("subwoofer"), dict) else None
        subwoofers = body.get("subwoofers") if isinstance(body.get("subwoofers"), dict) else None
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid JSON body, expected {"mode": <string>, "subwoofer": <object?>, "subwoofers": <object?>}')

    if measurement_sr_session is not None and measurement_sr_session.has_active_jobs:
        raise HTTPException(status_code=423, detail="Measurement is active; output mode switch is locked")

    try:
        target = prepare_audio_output_mode(mode, subwoofer, subwoofers)
        target_mode = str(target["config"].get("mode") or "").strip()

        def mode_transition_guard(target_overview: dict) -> float:
            runtime_snapshot = runtime.dsp_runtime.snapshot() if runtime.dsp_runtime else {}
            previous_gain = float(runtime_snapshot.get("output_gain_db") or 0.0)
            current_layout = ((runtime_snapshot.get("config") or {}).get("layout") or [])
            target_layout = DSPRuntimeConfig.from_overview(target_overview).layout
            current_peak_gain = max((float(channel.get("gain_db", 0.0)) for channel in current_layout), default=0.0)
            target_peak_gain = max((float(channel.get("gain_db", 0.0)) for channel in target_layout), default=0.0)
            positive_gain_delta = max(0.0, target_peak_gain - current_peak_gain)
            return min(0.0, previous_gain - max(1.0, positive_gain_delta + 1.0))

        # A same-mode request is a pure DSP parameter change (crossover, level,
        # alignment, polarity, highpass).  It changes no routing, samplerate or
        # graph topology, so it must not enter the Coordinator's muted
        # output-mode transition.  Restore the pre-coordinator direct sync:
        # persist the settings and push them into the native helper without
        # ever closing the hardware-output gate.
        current_mode = str(
            (samplerate._load_audio_output_mode().get("mode") or OUTPUT_MODE_STEREO)
        ).strip()
        if target_mode == current_mode:
            previous_overview = get_audio_output_overview()
            try:
                previous_mode_raw = samplerate._audio_output_mode_path().read_bytes()
            except OSError:
                previous_mode_raw = None
            result = persist_audio_output_mode(target["config"])
            if runtime.dsp_runtime is None:
                await dsp_orchestrator.sync_runtime(result, reason="output-mode-params", retry_on_stale=True)
            else:
                try:
                    await runtime.dsp_runtime.guarded_rebuild(
                        result,
                        guard_db=mode_transition_guard(result),
                        apply_candidate=lambda: None,
                        apply_previous=lambda: None,
                        settle_seconds=0.0,
                    )
                except Exception:
                    try:
                        if previous_mode_raw is None:
                            try:
                                samplerate._audio_output_mode_path().unlink(missing_ok=True)
                            except OSError:
                                logger.exception("Failed to remove persisted output mode after same-mode transition failure")
                        elif samplerate._audio_output_mode_path().read_bytes() != previous_mode_raw:
                            samplerate._audio_output_mode_path().write_bytes(previous_mode_raw)
                    except OSError:
                        logger.exception("Failed to restore persisted output mode after same-mode transition failure")
                    try:
                        await runtime.dsp_runtime.sync(previous_overview)
                    except Exception:
                        logger.exception("Failed to restore native DSP after same-mode transition failure")
                    raise
            result = with_subwoofer_derived_delays(result)
            if runtime.dsp_runtime is not None:
                result["output_mode"] = {
                    **(result.get("output_mode") or {}),
                    "runtime": runtime.dsp_runtime.snapshot(),
                }
            await dsp_orchestrator.refresh_peak_monitor_after_effects_change("audio-output-mode-params")
            return result

        context = await _coordinator_current_playback_context()
        status = get_samplerate_status()
        target_rate = status.get("active_rate")
        if not isinstance(target_rate, int) or target_rate <= 0:
            target_rate = status.get("force_rate")
        if not isinstance(target_rate, int) or target_rate <= 0:
            raise RuntimeError("current hardware sample rate is unavailable")
        await _run_coordinated_transition(TransitionRequest(
            operation="output-mode-switch",
            source=str(context.get("source") or "local"),
            target_rate=target_rate,
            target_url=context.get("target_url"),
            target_track=dict(context.get("target_track") or {}),
            should_play=bool(context.get("should_play")),
            rate_change=False,
            reload_source=False,
            detail="api-audio-output-mode",
            output_mode_target=dict(target["overview"]),
            output_mode_config=dict(target["config"]),
        ))
        result = with_subwoofer_derived_delays(get_audio_output_overview())
        if runtime.dsp_runtime is not None:
            result["output_mode"] = {
                **(result.get("output_mode") or {}),
                "runtime": runtime.dsp_runtime.snapshot(),
            }
        await dsp_orchestrator.refresh_peak_monitor_after_effects_change("audio-output-mode-switch")
        return result
    except ValueError as exc:
        raise bad_request(exc)
    except PlaybackTransitionFailure as exc:
        raise _transition_error_http(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save audio output mode: {exc}")


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


async def _pause_all_app_playback_for_external_input() -> None:
    try:
        if runtime.player_instance and runtime.player_instance._running:
            await _drain_worker(runtime.player_instance.stop_playback)
            await manager.broadcast({"type": "playback", "data": build_playback_payload(runtime.player_instance.state)})
            released = await media_readiness.wait_for_pipewire_mpv_release()
            if not released:
                await asyncio.sleep(SOURCE_HANDOFF_SETTLE_MS / 1000)
    except Exception:
        pass
    try:
        spotify_state = await get_spotify_ui_state()
        if spotify_state.get("status") == "Playing":
            data = await spotify_pause()
            await broadcast_spotify_state(data)
    except Exception:
        pass


@app.post("/api/audio/source-mode")
async def save_audio_source_selection_route(request: Request):
    try:
        body = await request.json()
        mode = str(body.get("mode", "")).strip()
        input_key = str(body.get("inputKey", body.get("input_key", ""))).strip() or None
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid JSON body, expected {"mode": <string>, "inputKey": <string?>}')

    try:
        try:
            previous_source_selection = samplerate._audio_source_selection_path().read_bytes()
        except OSError:
            previous_source_selection = None
        previous_source_state = samplerate._load_audio_source_selection()
        result = set_audio_source_selection(mode, input_key)
        try:
            result = await external_input.sync(result)
            result = await bluetooth_input.sync(result)
        except Exception:
            try:
                if previous_source_selection is None:
                    try:
                        samplerate._audio_source_selection_path().unlink(missing_ok=True)
                    except OSError:
                        logger.exception("Failed to remove persisted source mode after routing failure")
                elif samplerate._audio_source_selection_path().read_bytes() != previous_source_selection:
                    samplerate._audio_source_selection_path().write_bytes(previous_source_selection)
            except OSError:
                logger.exception("Failed to restore persisted source mode after routing failure")
            try:
                restored = set_audio_source_selection(
                    str(previous_source_state.get("mode") or SOURCE_MODE_APP_PLAYBACK),
                    previous_source_state.get("selected_input_key"),
                )
                try:
                    restored = await external_input.sync(restored)
                    restored = await bluetooth_input.sync(restored)
                except Exception:
                    logger.exception("Failed to re-sync routing after source-mode rollback")
            except Exception:
                logger.exception("Failed to restore previous source selection after routing failure")
            raise
        if result.get("mode") in {SOURCE_MODE_EXTERNAL_INPUT, SOURCE_MODE_BLUETOOTH_INPUT}:
            await _pause_all_app_playback_for_external_input()
        await peak_monitor_coordinator.sync_source_mode_state(result)
        return result
    except ValueError as exc:
        raise bad_request(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save source mode: {exc}")


def _require_dsp_manager():
    global dsp_manager
    if not dsp_manager:
        raise HTTPException(status_code=503, detail="DSP manager not available")
    return dsp_manager


async def _load_dsp_preset(
    preset_name: str, *, convolver_sample_rate_hz: int | None = None,
    _locks_held: bool = False, _rate_lock_held: bool = False,
) -> None:
    """Serialize preset loads against threaded DSP mutations.

    load_preset() also synchronizes global extras into the preset and is
    therefore not read-only: it must never run concurrently with a threaded
    IR/preset mutation.  Lock order: the canonical volume write lock is
    acquired first, then the DSP mutation lock.  Callers that already hold
    both locks pass ``_locks_held=True``.
    ``_rate_lock_held`` is forwarded to the runtime sync so a caller that
    already owns the measurement sample-rate session lock (the measurement
    entry) does not re-enter it.
    """
    volume_lock = None
    if not _locks_held:
        volume_lock = _canonical_volume_write_lock()
        await volume_lock.acquire()
    try:
        if _locks_held:
            await _load_preset_locked(preset_name, convolver_sample_rate_hz=convolver_sample_rate_hz,
                                      _rate_lock_held=_rate_lock_held)
        else:
            async with _dsp_mutation_lock():
                await _load_preset_locked(preset_name, convolver_sample_rate_hz=convolver_sample_rate_hz,
                                          _rate_lock_held=_rate_lock_held)
    finally:
        if volume_lock is not None:
            volume_lock.release()


async def _restore_volume_state(manager, start: volume_contract.VolumeState) -> None:
    await _drain_worker(set_output_volume, int(start.master_percent))
    if runtime.dsp_runtime is not None and runtime.dsp_runtime.snapshot().get("active"):
        await runtime.dsp_runtime.set_output_gain_db(float(start.dsp_guard_db))
    if not manager:
        return
    extras = copy.deepcopy(manager.load_global_extras())
    extras.setdefault("loudness", {}).setdefault("params", {})["volumeDb"] = float(start.volume_db)
    extras.setdefault("loudness", {})["enabled"] = bool(start.loudness_enabled)
    save = getattr(manager, "save_global_extras", None)
    if callable(save):
        save(extras)
    if (manager.get_active_preset() or "") != start.preset:
        if hasattr(manager, "active_preset"):
            manager.active_preset = start.preset
        elif getattr(manager, "state_store", None) is not None:
            manager.state_store.write(
                "active.json",
                {"schema": "fxroute.dsp.active", "version": 1, "preset": start.preset},
            )


async def _load_preset_locked(
    preset_name: str, *, convolver_sample_rate_hz: int | None = None,
    _rate_lock_held: bool = False,
) -> None:
    manager = _require_dsp_manager()
    start = await _volume_state_for_manager(manager)
    try:
        await _drain_worker(
            manager.load_preset,
            preset_name,
            convolver_sample_rate_hz=convolver_sample_rate_hz,
        )
        await dsp_orchestrator.sync_runtime(reason="native-dsp-preset-load",
                                            _rate_lock_held=_rate_lock_held)
    except Exception:
        try:
            if (manager.get_active_preset() or "") != start.preset:
                try:
                    await _drain_worker(manager.load_preset, start.preset)
                except Exception:
                    logger.exception("Failed to reload previous preset after preset load failure")
                    if hasattr(manager, "active_preset"):
                        manager.active_preset = start.preset
            await dsp_orchestrator.sync_runtime(reason="native-dsp-preset-load-rollback",
                                                _rate_lock_held=_rate_lock_held)
        except Exception:
            logger.exception("Failed to restore previous preset after preset load failure")
        raise


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
        asyncio.create_task(_run_claimed_refresh())
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
    try:
        body = await request.json()
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
    if not downloader:
        raise HTTPException(status_code=503, detail="Downloader not available")
    try:
        body = await request.json()
        url = body.get("url")
        if not url:
            raise HTTPException(status_code=400, detail="URL is required")
        filename = downloader.download(url)
        return {"status": "started", "filename": filename}
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise internal_error("Download start failed", e)

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
    if downloader and downloader.active_download:
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
        request_origin_is_trusted=lambda req: _request_origin_is_trusted(req),
        coordinator_rate_change=lambda rate: _coordinator_rate_change(rate),
        run_coordinated_transition=lambda req: _run_coordinated_transition(req),
        get_current_playback_owner=lambda: playback_state.current_playback_owner,
        spotify_playerctl_watch=spotify_playerctl_watch,
        api_spotify_play=lambda: api_spotify_play(),
        api_spotify_toggle=lambda: api_spotify_toggle(),
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
    if not _request_origin_is_trusted(request):
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    try:
        body = await request.json()
    except Exception:
        body = {}
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
    body = await request.json()
    position = float(body.get("position", 0))
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
dsp_orchestrator = DspOrchestrator(_make_dsp_orchestration_deps())

if __name__ == "__main__":
    settings = get_settings()
    run_server()
