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
import math
import subprocess
import tempfile
import playback.queue as playback_queue
import audio.samplerate_orchestration as samplerate_orchestration
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional
from urllib.parse import quote, unquote, urlparse

import uvicorn
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import get_settings
from library.sources import MusicLibraryManager
from radio.metadata import RadioMetadataService

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
COVER_CACHE_DIR = BASE_DIR / "media" / "cache" / "covers"
TOP40_COVER_IMAGE = STATIC_DIR / "Top40.png"
UPDATE_SCRIPT = BASE_DIR / "scripts" / "update_fxroute.sh"

# Cooldown to prevent rapid mpv IPC flooding (ms)
PLAY_COMMAND_COOLDOWN_MS = 400
LOCAL_TRACK_SWITCH_SETTLE_MS = 260
PIPEWIRE_HANDOFF_RELEASE_TIMEOUT_MS = 1800
PIPEWIRE_HANDOFF_POLL_INTERVAL_MS = 50
# Bounded window for the idempotent MPV->DSP ingress link reconciliation
# after the source ports appeared (link creation plus readback confirm).
MPV_LINK_REPAIR_TIMEOUT_MS = 1500
SPOTIFY_SINK_INPUT_RATE_TIMEOUT_MS = 1800
PEAK_MONITOR_RESTART_SETTLE_MS = 320
PEAK_MONITOR_RATE_MATCH_TIMEOUT_MS = 900
RADIO_POST_LOAD_RATE_TIMEOUT_MS = 3000
RADIO_POST_LOAD_RATE_STABILITY_POLLS = 3
# Bounded read-only budget for the MPV stream's PipeWire output ports to
# appear after a staged cold radio loadfile: mpv publishes mpv:output_FL/FR
# only once the network stream actually opened (observed ~4 s cold start).
# The MPV->DSP ingress link repair must never run while the ports are
# absent; this is source-startup readiness, not a fixed sleep.
RADIO_SOURCE_PORT_READINESS_TIMEOUT_MS = 4500
# Bounded readback wait for the native DSP ports after a rate switch or a
# missing-graph repair. No fixed sleeps: the handoff polls pw-link until the
# fxroute_dsp input/output ports are exposed, then starts/syncs the helper.
PLAYBACK_HANDOFF_EE_PORT_TIMEOUT_MS = 5000
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


def _list_sink_inputs() -> list[dict]:
    """Thin wrapper: pactl sink-input parsing lives in sink_inputs (REFACTOR-011)."""
    return sink_inputs.list_sink_inputs()


def _list_mpv_sink_inputs() -> list[dict]:
    return [
        entry
        for entry in _list_sink_inputs()
        if (entry.get("properties") or {}).get("application.name") == "mpv"
        or (entry.get("properties") or {}).get("application.id") == "mpv"
        or (entry.get("properties") or {}).get("node.name") == "mpv"
    ]


def _list_spotify_sink_inputs() -> list[dict]:
    return [
        entry
        for entry in _list_sink_inputs()
        if str((entry.get("properties") or {}).get("application.name") or "").lower() == "spotify"
        or str((entry.get("properties") or {}).get("application.id") or "").lower() == "spotify"
        or str((entry.get("properties") or {}).get("node.name") or "").lower() == "spotify"
        or (entry.get("properties") or {}).get("media.name") == "Spotify"
    ]


def _spotify_sink_input_observation(
    entries: list[dict],
    *,
    expected_rate: int | None = None,
    preferred_identity: object | None = None,
) -> tuple[object, int] | None:
    """Select one Spotify sink input and retain an identity for stability checks."""
    candidates: list[tuple[object, int]] = []
    for entry in entries:
        corked = entry.get("corked")
        if isinstance(corked, str):
            corked = corked.strip().lower() in {"1", "true", "yes", "on"}
        if corked:
            # A corked input is an old/paused PipeWire stream.  It must not
            # validate a new Playing entry or hide a newly created active
            # input with a different rate.
            continue
        rate = entry.get("sample_rate")
        if isinstance(rate, int) and rate > 0:
            properties = entry.get("properties") or {}
            identity: object = entry.get("id")
            if identity is None:
                identity = entry.get("index")
            if identity is None:
                identity = (
                    properties.get("node.name"),
                    properties.get("application.name") or properties.get("application.id"),
                    properties.get("media.name"),
                )
            candidates.append((identity, rate))
    if not candidates:
        return None

    selected_identity, selected_rate = candidates[0]
    preferred = next(
        (
            candidate
            for candidate in candidates
            if preferred_identity is not None and candidate[0] == preferred_identity
        ),
        None,
    )
    expected = next(
        (
            candidate
            for candidate in candidates
            if isinstance(expected_rate, int)
            and expected_rate > 0
            and candidate[1] == expected_rate
        ),
        None,
    )
    if preferred is not None and (expected_rate is None or preferred[1] == expected_rate):
        selected_identity, selected_rate = preferred
    elif expected is not None:
        # A stale preferred input must not mask a newly appeared input that
        # already has the rate required by the Coordinator commit contract.
        selected_identity, selected_rate = expected
    elif preferred is not None:
        selected_identity, selected_rate = preferred
    return selected_identity, selected_rate


async def _wait_for_sink_input_release(list_fn, timeout_ms: int) -> bool:
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while time.monotonic() <= deadline:
        if not list_fn():
            return True
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
    return not list_fn()


async def _wait_for_pipewire_mpv_release(timeout_ms: int = PIPEWIRE_HANDOFF_RELEASE_TIMEOUT_MS) -> bool:
    return await _wait_for_sink_input_release(_list_mpv_sink_inputs, timeout_ms)


async def _wait_for_pipewire_spotify_release(
    timeout_ms: int = PIPEWIRE_HANDOFF_RELEASE_TIMEOUT_MS,
) -> bool:
    # A paused Spotify client may retain a corked historical sink-input.  That
    # input is not producing audio and must not block a source handoff.  Only
    # active, audible Spotify inputs are relevant to the quiescence contract.
    def active_spotify_inputs() -> list[dict]:
        return _active_unmuted_sink_inputs(_list_spotify_sink_inputs())

    return await _wait_for_sink_input_release(active_spotify_inputs, timeout_ms)


async def _wait_for_spotify_sink_input_samplerate(
    *,
    expected_rate: int | None = None,
    timeout_ms: int = SPOTIFY_SINK_INPUT_RATE_TIMEOUT_MS,
) -> int:
    """Read a stable Spotify stream rate before an entry transition commits."""
    if not isinstance(expected_rate, int) or expected_rate <= 0:
        raise RuntimeError(f"Spotify entry has no valid expected samplerate: {expected_rate}")
    poll_interval_ms = max(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS, 1)
    max_polls = max(1, math.ceil(max(timeout_ms, 0) / poll_interval_ms) + 1)
    last_observation: tuple[object, int] | None = None
    stable_polls = 0
    last_rate: int | None = None
    for poll_index in range(max_polls):
        try:
            observation = _spotify_sink_input_observation(
                _list_spotify_sink_inputs(),
                expected_rate=expected_rate,
                preferred_identity=(last_observation[0] if last_observation else None),
            )
        except Exception:
            observation = None
        if observation is not None:
            identity, rate = observation
            last_rate = rate
            if rate == expected_rate and observation == last_observation:
                stable_polls += 1
            elif rate == expected_rate:
                stable_polls = 1
            else:
                # A wrong/transient rate is observed but never accepted as a
                # stable entry result.  The counter also resets on an input
                # identity change so an old Spotify stream cannot validate a
                # newly appeared one.
                stable_polls = 0
            last_observation = (identity, rate)
            if rate == expected_rate and stable_polls >= SPOTIFY_SINK_INPUT_RATE_STABILITY_POLLS:
                return rate
        else:
            # A disappearing input is a new stream boundary.  Do not carry
            # stability across that gap, even if the next input reuses the
            # same PipeWire identity.
            last_observation = None
            stable_polls = 0
        if poll_index + 1 < max_polls:
            await asyncio.sleep(poll_interval_ms / 1000)
    raise RuntimeError(
        "Spotify sink-input samplerate did not become readable and stable "
        f"at the expected rate within {timeout_ms} ms "
        f"(expected={expected_rate} last={last_rate})"
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
    playback_active = _is_local_playback_active(local_state) or _is_spotify_playback_active(spotify_state)
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


def _has_local_footer_context(state: dict | None) -> bool:
    state = state or {}
    track = playback_state.current_track_info or state.get("current_track") or {}
    source = (track or {}).get("source")
    if source not in {"local", "radio"}:
        return False
    return bool(
        state.get("current_file")
        or state.get("playing")
        or state.get("paused")
        or state.get("ended")
    )


def _get_authoritative_footer_owner(player_state: dict | None = None, spotify_state: dict | None = None) -> str:
    player_state = player_state or (runtime.player_instance.state if runtime.player_instance else {})
    spotify_state = spotify_state or playback_state.latest_spotify_state or {}

    # A loaded/paused MPV file is only fallback context.  First resolve the
    # sources that are actually producing audio; otherwise a paused Local
    # track would mask a currently playing Spotify client (and vice versa).
    local_active = _is_local_playback_active(player_state)
    spotify_active = _is_spotify_playback_active(spotify_state)
    if spotify_active and not local_active:
        playback_state.current_footer_owner = "spotify"
        return playback_state.current_footer_owner
    if local_active and not spotify_active:
        playback_state.current_footer_owner = "local"
        return playback_state.current_footer_owner

    if local_active and spotify_active:
        # This is an inconsistent dual-active readback.  Keep the already
        # committed owner when possible; it is deterministic and avoids a UI
        # flip while the two source states converge.
        if playback_state.current_footer_owner in {"local", "spotify"}:
            return playback_state.current_footer_owner
        playback_state.current_footer_owner = "spotify"
        return playback_state.current_footer_owner

    # Neither source is active.  The committed owner is the first fallback;
    # only an unowned loaded MPV context may establish a local fallback.
    if playback_state.current_footer_owner in {"local", "spotify"}:
        return playback_state.current_footer_owner
    if _has_local_footer_context(player_state):
        playback_state.current_footer_owner = "local"
        return playback_state.current_footer_owner
    return playback_state.current_footer_owner or "local"




from models import (
    PlayRequest,
)
from playback.player import get_player, MPVNotInstalledError, normalize_stream_info
from radio.api import _station_api_payload, router as radio_api_router
from radio.stations import get_stations
import audio.sink_inputs as sink_inputs
import playback.state as playback_state_helpers
from playback.state import PlaybackState
import audio.samplerate as samplerate
from library.core import (
    LibraryScanner,
)
from downloader import Downloader
from dsp.manager import DSPManager
from dsp.runtime import DSPRuntime, DSPRuntimeConfig, BassManagementConfig, _contains_link
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
    SPOTIFY_PREARM_SAMPLE_RATE_HZ,
    SPOTIFY_SINK_INPUT_RATE_STABILITY_POLLS,
    SpotifyPlayerctlWatch,
    SpotifyWatchDependencies,
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
    set_audio_output_selection,
    set_audio_source_selection,
    set_bluetooth_receiver_enabled,
)
from playback.spotify import (
    playerctl_available,
    spotify_installed,
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
import dsp.effects_extras
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
    # Serializes canonical volume writes (/api/volume, /api/spotify/volume) so
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


radio_reconnect = RadioReconnect(RadioReconnectDependencies(
    get_player_instance=lambda: runtime.player_instance,
    get_playback_state=lambda: playback_state,
    request_coordinated_recovery=lambda *args, **kwargs: _request_coordinated_recovery(*args, **kwargs),
))

silent_active_recovery = SilentActiveRecovery(SilentActiveDependencies(
    get_peak_monitor=lambda: runtime.peak_monitor,
    get_player_instance=lambda: runtime.player_instance,
    get_current_track_info=lambda: playback_state.current_track_info,
    get_current_footer_owner=lambda: playback_state.current_footer_owner,
    get_spotify_ui_state=lambda *args, **kwargs: get_spotify_ui_state(*args, **kwargs),
    list_mpv_sink_inputs=lambda: _list_mpv_sink_inputs(),
    list_spotify_sink_inputs=lambda: _list_spotify_sink_inputs(),
    list_all_sink_inputs=lambda: _list_sink_inputs(),
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
))

samplerate_drift = SamplerateDriftObserver(SamplerateDriftDependencies(
    get_current_track_info=lambda: playback_state.current_track_info,
    get_player_instance=lambda: runtime.player_instance,
    coordinator_source_rate=lambda *args, **kwargs: _coordinator_source_rate(*args, **kwargs),
    get_player_audio_samplerate=lambda: _get_player_audio_samplerate(),
    get_samplerate_status=lambda: get_samplerate_status(),
    playback_transition_is_active=lambda: _playback_transition_is_active(),
    is_measurement_window_open=lambda: _is_measurement_window_open(),
    measurement_session_active=lambda: measurement_sr_session is not None and measurement_sr_session.active,
    measurement_audio_graph_owned=lambda: _measurement_audio_graph_owned(),
    request_coordinated_recovery=lambda *args, **kwargs: _request_coordinated_recovery(*args, **kwargs),
))

spotify_playerctl_watch = SpotifyPlayerctlWatch(SpotifyWatchDependencies(
    get_playback_state=lambda: playback_state,
    get_spotify_ui_state=lambda *args, **kwargs: get_spotify_ui_state(*args, **kwargs),
    list_spotify_sink_inputs=lambda: _list_spotify_sink_inputs(),
    spotify_sink_input_observation=lambda *args, **kwargs: _spotify_sink_input_observation(*args, **kwargs),
    request_coordinated_recovery=lambda *args, **kwargs: _request_coordinated_recovery(*args, **kwargs),
    schedule_spotify_state_refresh=lambda reason: _schedule_spotify_state_refresh(reason),
))
radio_metadata_service = RadioMetadataService()
# queue_advancing is a reentrancy/dispatch guard for
# on_player_state_change and deliberately not queue state: the queue
# state (list, original order, index, mode, loop, shuffle, single-track-
# loop) lives exclusively in playback.queue.PlaybackQueue.
queue_advancing = False

configure_library_api_runtime(LibraryApiRuntime(
    get_scanner=lambda: runtime.music_library.scanner,
    get_settings=lambda: settings,
    run_blocking=_drain_worker,
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
        playback_graph_diagnosis=lambda *a, **k: _playback_graph_diagnosis(*a, **k),
        log_playback_graph_diagnosis=lambda *a, **k: _log_playback_graph_diagnosis(*a, **k),
        measurement_restore_intent_matches_live_state=lambda *a, **k: _measurement_restore_intent_matches_live_state(*a, **k),
        spotify_snapshot_identity_values=lambda *a, **k: _spotify_snapshot_identity_values(*a, **k),
        spotify_target_track_from_state=lambda *a, **k: _spotify_target_track_from_state(*a, **k),
        get_player_audio_samplerate=lambda *a, **k: _get_player_audio_samplerate(*a, **k),
        pulse_suspend_sink_for_samplerate=lambda *a, **k: samplerate.pulse_suspend_sink_for_samplerate(*a, **k),
        audio_output_overview_with_effective_rate=lambda *a, **k: samplerate.audio_output_overview_with_effective_rate(*a, **k),
        spotify_prearm_sample_rate_hz=SPOTIFY_PREARM_SAMPLE_RATE_HZ,
        pipewire_handoff_poll_interval_ms=PIPEWIRE_HANDOFF_POLL_INTERVAL_MS,
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


def _set_runtime_current_footer_owner(value: str) -> None:
    playback_state.current_footer_owner = value


def _set_runtime_track_context(current: dict, last: dict) -> None:
    playback_state.set_track_context(current, last)


def make_playback_runtime_deps() -> PlaybackRuntimeDependencies:
    """Late-bound wiring for ``FxrouteTransitionRuntime`` (playback/runtime.py).

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
        set_footer_owner=_set_runtime_current_footer_owner,
        queue=lambda: playback_queue.queue,
        player_is_running=lambda *a, **k: _player_is_running(*a, **k),
        load_player_paused=lambda *a, **k: _load_player_paused(*a, **k),
        wait_for_player_current_file=lambda *a, **k: _wait_for_player_current_file(*a, **k),
        wait_for_player_audio_samplerate=lambda *a, **k: _wait_for_player_audio_samplerate(*a, **k),
        get_player_audio_samplerate=lambda *a, **k: _get_player_audio_samplerate(*a, **k),
        wait_for_radio_live_rate_after_load=lambda *a, **k: _wait_for_radio_live_rate_after_load(*a, **k),
        wait_for_pipewire_mpv_release=lambda *a, **k: _wait_for_pipewire_mpv_release(*a, **k),
        wait_for_pipewire_spotify_release=lambda *a, **k: _wait_for_pipewire_spotify_release(*a, **k),
        wait_for_spotify_sink_input_samplerate=lambda *a, **k: _wait_for_spotify_sink_input_samplerate(*a, **k),
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
        coordinator_source_rate=lambda *a, **k: _coordinator_source_rate(*a, **k),
        coordinator_target_rate=lambda *a, **k: _coordinator_target_rate(*a, **k),
        spotify_pause=lambda *a, **k: spotify_pause(*a, **k),
        get_spotify_ui_state=lambda *a, **k: get_spotify_ui_state(*a, **k),
        is_spotify_playback_active=lambda *a, **k: _is_spotify_playback_active(*a, **k),
        has_local_footer_context=lambda *a, **k: _has_local_footer_context(*a, **k),
        pause_spotify_for_local_playback_broadcast=lambda *a, **k: pause_spotify_for_local_playback_broadcast(*a, **k),
        pause_local_playback_for_spotify_broadcast=lambda *a, **k: pause_local_playback_for_spotify_broadcast(*a, **k),
        mark_player_state_authoritative=lambda *a, **k: _mark_player_state_authoritative(*a, **k),
        spotify_snapshot_identity_values=lambda *a, **k: _spotify_snapshot_identity_values(*a, **k),
        measurement_restore_intent_matches_live_state=lambda *a, **k: _measurement_restore_intent_matches_live_state(*a, **k),
        dsp_mutation_lock=lambda: _dsp_mutation_lock(),
        drain_worker=lambda *a, **k: _drain_worker(*a, **k),
        load_dsp_preset=lambda *a, **k: _load_dsp_preset(*a, **k),
        sync_dsp_runtime=lambda *a, **k: dsp_orchestrator.sync_runtime(*a, **k),
        helper_argument_sample_rate=dsp_orchestration.helper_argument_sample_rate,
        playback_graph_diagnosis=lambda *a, **k: _playback_graph_diagnosis(*a, **k),
        measurement_session_link_loss_is_repairable=lambda *a, **k: _measurement_session_link_loss_is_repairable(*a, **k),
        coordinator_reconcile_subwoofer_links_only=lambda *a, **k: _coordinator_reconcile_subwoofer_links_only(*a, **k),
        repair_stereo_output_links_once=lambda *a, **k: _repair_stereo_output_links_once(*a, **k),
        coordinator_establish_effects_and_helper=lambda *a, **k: _coordinator_establish_effects_and_helper(*a, **k),
        ensure_mpv_to_dsp_links=lambda *a, **k: _ensure_mpv_to_dsp_links(*a, **k),
        playback_graph_links_complete=lambda *a, **k: _playback_graph_links_complete(*a, **k),
        log_playback_graph_diagnosis=lambda *a, **k: _log_playback_graph_diagnosis(*a, **k),
        coordinator_reconcile_post_start_graph=lambda *a, **k: _coordinator_reconcile_post_start_graph(*a, **k),
    )


playback_queue.configure_playback_queue(playback_queue.PlaybackQueueDependencies(
    player=lambda: runtime.player_instance,
    run_transition=lambda *a, **k: _run_coordinated_transition(*a, **k),
    commit_coordinated_track=lambda *a, **k: _commit_coordinated_track(*a, **k),
    get_current_track_info=lambda: playback_state.current_track_info,
    set_track_context=_set_runtime_track_context,
    transition_is_active=lambda: _playback_transition_is_active(),
    player_is_running=lambda *a, **k: _player_is_running(*a, **k),
    wait_for_player_current_file=lambda *a, **k: _wait_for_player_current_file(*a, **k),
    coordinator_target_rate=lambda *a, **k: _coordinator_target_rate(*a, **k),
    coordinator_rate_change=lambda *a, **k: _coordinator_rate_change(*a, **k),
    sample_rate_policy_is_auto=lambda: _sample_rate_policy_is_auto(),
    transition_error_http=lambda exc: _transition_error_http(exc),
    get_tracks=lambda: runtime.music_library.scanner.get_tracks(),
    build_playback_payload=lambda *a, **k: build_playback_payload(*a, **k),
))


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


def _publish_playback_context_commit(commit_token: str | None) -> None:
    """Publish the playback-context token exactly once at the app boundary.

    Synchronous: callers invoke this immediately after all globals of the new
    authoritative playback context were committed and before any further
    await, so the Ended-Waiter can never observe a window with a new token
    but old playback globals (or vice versa).
    """
    playback_state.publish_playback_context_commit(commit_token)


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
    playback_state.current_footer_owner = "spotify" if source == "spotify" else "local"
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


async def _wait_for_player_current_file(expected_url: str | None, timeout_ms: int = 1600) -> bool:
    if not expected_url or not runtime.player_instance:
        return False
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while time.monotonic() <= deadline:
        state = runtime.player_instance.state
        # ``loadfile`` sets ``current_file`` optimistically before mpv has
        # actually opened the file, so the file path alone is not enough: a
        # follow-up seek (or other mutation) would race the load and mpv
        # rejects it with "error running command".  The source is ready once
        # mpv reports a positive ``duration`` (known-length files/streams) or
        # fires the ``file-loaded`` event (live/unknown-length streams that
        # never report a duration).
        if state.get("current_file") == expected_url and (
            float(state.get("duration") or 0.0) > 0.0
            or bool(state.get("file_loaded"))
        ):
            return True
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
    return False


def _active_unmuted_sink_inputs(entries: list[dict]) -> list[dict]:
    return sink_inputs.active_unmuted_sink_inputs(entries)


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


async def _wait_for_dsp_output_ports(timeout_ms):
    return await playback_orchestration.configured().wait_for_dsp_output_ports(timeout_ms)



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


async def _mpv_source_ports_present() -> bool:
    """Read-only check: are the MPV stream ports and EE sink ports exposed?

    The mpv PipeWire stream (and with it mpv:output_FL/FR) is published only
    once the stream actually opened after a staged ``loadfile``.  This check
    is the source-startup readiness predicate: while it is false, no link
    mutation may run.
    """
    try:
        links_text = await pw_link.run_pw_link_command("-io")
    except Exception:
        return False
    return all(
        port in links_text
        for port in (
            "mpv:output_FL",
            "mpv:output_FR",
            "fxroute_dsp_sink:playback_FL",
            "fxroute_dsp_sink:playback_FR",
        )
    )


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


def _get_player_audio_samplerate() -> Optional[int]:
    if not runtime.player_instance or not runtime.player_instance._running:
        return None
    try:
        audio_params = runtime.player_instance.get_property("audio-params")
    except Exception as exc:
        logger.debug("Failed to read mpv audio-params: %s", exc)
        return None
    if not isinstance(audio_params, dict):
        return None
    rate = audio_params.get("samplerate")
    return rate if isinstance(rate, int) and rate > 0 else None


async def _wait_for_player_audio_samplerate(
    timeout_ms: int = PEAK_MONITOR_RATE_MATCH_TIMEOUT_MS,
    *,
    expected_url: str | None = None,
) -> Optional[int]:
    rate = _get_player_audio_samplerate()
    state = runtime.player_instance.state if runtime.player_instance else {}
    if rate and (not expected_url or state.get("current_file") == expected_url):
        return rate
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while time.monotonic() <= deadline:
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
        state = runtime.player_instance.state if runtime.player_instance else {}
        if expected_url and state.get("current_file") != expected_url:
            continue
        rate = _get_player_audio_samplerate()
        if rate:
            return rate
    return None


async def _wait_for_radio_live_rate_after_load(
    previous_rate: Optional[int],
    transition_generation: int,
    *,
    timeout_ms: int = RADIO_POST_LOAD_RATE_TIMEOUT_MS,
) -> Optional[int]:
    """Wait for the newly loaded station's decoded rate while mpv is paused.

    Accepts a rate that differs from the pre-loadfile rate immediately (the
    new stream's rate), or a rate equal to the pre-loadfile rate once it
    stayed stable across RADIO_POST_LOAD_RATE_STABILITY_POLLS consecutive
    polls (same-rate station switch). Aborts on a stale transition
    generation and on timeout without a valid rate (caller falls back
    safely; no stale pre-loadfile params are used as evidence).
    """
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    stable_same = 0
    while time.monotonic() <= deadline:
        if transition_generation != playback_state.playback_transition_epoch:
            logger.info(
                "Radio post-load rate wait aborted: stale transition "
                "generation=%s current=%s",
                transition_generation,
                playback_state.playback_transition_epoch,
            )
            return None
        rate = _get_player_audio_samplerate()
        if isinstance(rate, int) and rate > 0:
            if previous_rate is None or rate != previous_rate:
                return rate
            stable_same += 1
            if stable_same >= RADIO_POST_LOAD_RATE_STABILITY_POLLS:
                return rate
        else:
            stable_same = 0
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
    logger.warning(
        "Radio post-load rate wait timed out after %sms: previous_rate=%s",
        timeout_ms,
        previous_rate,
    )
    return None




def ensure_local_source_volume() -> None:
    if not runtime.player_instance or not runtime.player_instance._running:
        return
    try:
        runtime.player_instance.set_volume(100)
    except Exception as exc:
        logger.warning("Failed to pin MPV source volume to 100%%: %s", exc)


def get_output_volume_safe(default: int = 100) -> int:
    if _loudness_owns_volume():
        try:
            loudness = dsp_manager.load_global_extras().get("loudness", {})
            volume_db = float(loudness.get("params", {}).get("volumeDb", 0.0))
            return dsp_manager.loudness_percent_from_db(volume_db)
        except Exception:
            logger.warning("Failed to read Loudness volume, falling back to system volume", exc_info=True)
    return get_status_volume(default)


def _loudness_owns_volume() -> bool:
    """Return whether Loudness owns the canonical attenuation.

    Ownership follows the active signal path: Direct bypasses every global
    helper, so the persisted ``loudness.enabled`` flag is not enough.
    """
    if not dsp_manager:
        return False
    try:
        extras = dsp_manager.load_global_extras()
        active = dsp_manager.get_active_preset() or ""
        return volume_contract.loudness_in_path(
            active, bool((extras.get("loudness") or {}).get("enabled"))
        )
    except Exception:
        return False


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
        if volume_contract.loudness_in_path(preset, enabled):
            live_master = 100
        else:
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


async def _apply_volume_actions(
    actions, extras=None, *, persist_extras: bool = True
):
    """Apply planned master/Loudness/guard writes. Caller holds the volume locks."""
    if extras is None and dsp_manager:
        extras = dsp_manager.load_global_extras()
    extras_dirty = False
    for action in actions:
        if action.op == "set_volume_db":
            if extras is None:
                extras = {"loudness": {"enabled": False, "params": {}}}
            extras.setdefault("loudness", {}).setdefault("params", {})["volumeDb"] = float(action.value)
            extras_dirty = True
        elif action.op == "set_loudness_enabled":
            if extras is None:
                extras = {"loudness": {"params": {}}}
            extras.setdefault("loudness", {})["enabled"] = bool(action.value)
            extras_dirty = True
        elif action.op == "set_master":
            await _drain_worker(set_output_volume, int(round(float(action.value))))
        elif action.op == "set_guard":
            if runtime.dsp_runtime is not None and runtime.dsp_runtime.snapshot().get("active"):
                await runtime.dsp_runtime.set_output_gain_db(float(action.value))
    if extras_dirty and persist_extras and dsp_manager and extras is not None:
        dsp_manager.save_global_extras(extras)
    return extras


async def _set_canonical_output_volume(volume: float | int) -> dict[str, Any]:
    """Apply the one UI-volume contract for local, radio and Spotify.

    One canonical perceived volume.  Loudness owns it only while it is in
    the active path; otherwise the system master does.  The write is
    serialized against other canonical volume writes.  Lock order:
    canonical volume write lock first, then DSP mutation lock.
    """
    async with _canonical_volume_write_lock():
        requested = max(0, min(100, int(round(float(volume)))))
        async with _dsp_mutation_lock():
            start = await _volume_state_for_manager(dsp_manager, live_master=0)
            target = volume_contract.target_for(current=start, percent=requested)
            if target.loudness_in_path and dsp_manager:
                volume_result = await _drain_worker(
                    dsp_manager.set_loudness_volume_db, target.volume_db
                )
                if not volume_result.get("runtime_applied"):
                    await dsp_orchestrator.sync_runtime(reason="native-dsp-loudness-volume")
                await _drain_worker(set_output_volume, 100)
                return {
                    "volume": requested,
                    "loudnessVolumeDb": float(
                        volume_result["extras"]["loudness"]["params"]["volumeDb"]
                    ),
                    "loudness_enabled": True,
                }
            await _drain_worker(set_output_volume, requested)
            extras = dsp_manager.load_global_extras() if dsp_manager else None
            remaining = [
                action for action in volume_contract.plan_transition(start, target)
                if action.op not in {"set_master", "set_volume_db"}
            ]
            if remaining:
                await _apply_volume_actions(remaining, extras)
            return {
                "volume": requested,
                "loudnessVolumeDb": None,
                "loudness_enabled": False,
            }


async def _guarded_effects_transition(previous, candidate, persist_all_presets):
    """Apply extras through a guarded DSP rebuild. Master is pinned only if Loudness is in path."""
    overview = get_audio_output_overview()
    result_holder = {}
    start = await _volume_state_for_manager(dsp_manager)
    candidate_loudness = (candidate.get("loudness") or {})
    target = volume_contract.target_for(
        current=start,
        loudness_enabled=bool(candidate_loudness.get("enabled")),
    )
    if start.loudness_in_path or target.loudness_in_path:
        guard_db = dsp_manager.loudness_transition_guard_db(previous, candidate)
    else:
        guard_db = min(0.0, start.dsp_guard_db, volume_percent_to_db(start.master_percent))
    pin_master = target.loudness_in_path and int(start.master_percent) != 100
    restore_master = start.master_percent if pin_master else None
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
        before_ramp=(lambda: _drain_worker(set_output_volume, 100)) if pin_master else None,
        before_rollback_ramp=(lambda: _drain_worker(
            set_output_volume, restore_master)) if pin_master else None,
    )
    result = result_holder["result"]
    result["runtime_applied"] = True
    return result


async def get_spotify_ui_state(data: Optional[dict] = None) -> dict:
    status = dict(data or await spotify_get_status())
    source_volume = status.get("volume") if isinstance(status.get("volume"), (int, float)) else None
    status["source_volume"] = int(round(float(source_volume))) if source_volume is not None else None
    status["volume"] = get_output_volume_safe(status.get("source_volume") or 100)
    status["footer_owner"] = _get_authoritative_footer_owner(spotify_state=status)
    art_url = str(status.get("artwork_url") or status.get("artUrl") or "").strip()
    status["artwork_available"] = bool(art_url)
    status["artwork_url"] = art_url or None
    status["artwork_source"] = "spotify" if art_url else "none"
    return status


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
    if playback_state.current_track_info and playback_state.current_track_info.get("source") in {"local", "radio"}:
        player_state["source_volume"] = int(round(float(source_volume))) if source_volume is not None else None
    elif source_volume is not None:
        player_state["source_volume"] = int(round(float(source_volume)))
    player_state["volume"] = get_output_volume_safe(int(round(float(source_volume))) if source_volume is not None else 100)
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
    player_state["footer_owner"] = _get_authoritative_footer_owner(player_state=player_state)

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
        "vu_fresh": False,
        "vu_age_ms": None,
        "target": None,
        "last_over_at": None,
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
        and playback_state.current_track_info.get("source") == "local"
        and playback_queue.queue.mode != "native_mpv"
    ):
        queue_advancing = True
        try:
            if len(playback_queue.queue.tracks) > 1 and await playback_queue.queue.advance(transition_reason="queue auto-advance") == "advanced":
                return
            if playback_queue.queue.single_track_loop and playback_state.current_track_info and playback_state.current_track_info.get("url"):
                loop_track = dict(playback_state.current_track_info)
                loop_rate = _coordinator_target_rate("local", loop_track)
                try:
                    result = await _run_coordinated_transition(TransitionRequest(
                        operation="replay",
                        source="local",
                        target_rate=loop_rate,
                        target_url=loop_track.get("url"),
                        target_track=loop_track,
                        should_play=True,
                        rate_change=_coordinator_rate_change(loop_rate),
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
            active = bool(state.get("available") and (state.get("status") == "Playing" or playback_state.current_footer_owner == "spotify"))
            await asyncio.sleep(SPOTIFY_STATE_POLL_INTERVAL_SECONDS if active else SPOTIFY_STATE_IDLE_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Spotify metadata poll fallback failed: %s", exc)
            await asyncio.sleep(SPOTIFY_STATE_IDLE_POLL_INTERVAL_SECONDS)


async def _spotify_player_present(timeout: float = 0.8) -> bool:
    try:
        import shutil
        pc = shutil.which("playerctl")
        if not pc:
            return False
        proc = await asyncio.create_subprocess_exec(
            pc,
            "-l",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            return False
        players = stdout.decode(errors="ignore").splitlines()
        return any(player.strip().lower() == "spotify" for player in players)
    except Exception:
        return False


async def pause_spotify_for_local_playback_broadcast():
    playback_state.current_footer_owner = "local"
    if not await _spotify_player_present():
        playback_state.latest_spotify_state = {
            "available": playerctl_available(),
            "installed": spotify_installed(),
            "source": "spotify",
            "status": "Stopped",
            "footer_owner": "local",
        }
        await manager.broadcast({"type": "spotify", "data": playback_state.latest_spotify_state})
        return
    try:
        import shutil
        pc = shutil.which("playerctl")
        if pc:
            proc = await asyncio.create_subprocess_exec(pc, "--player=spotify", "pause")
            await asyncio.wait_for(proc.communicate(), timeout=3)
    except Exception:
        pass
    try:
        await broadcast_spotify_state()
    except Exception:
        pass


async def pause_local_playback_for_spotify_broadcast():
    playback_state.current_footer_owner = "spotify"
    try:
        if runtime.player_instance and runtime.player_instance._running:
            runtime.player_instance.stop_playback()
            playback_state.current_track_info = None
            await manager.broadcast({"type": "playback", "data": build_playback_payload(runtime.player_instance.state)})
            released = await _wait_for_pipewire_mpv_release()
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
            ensure_local_source_volume()
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
        if _loudness_owns_volume():
            set_output_volume(100)
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
        playback_state.coordinator_last_successful_commit_id = None
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
        logger.info("DSP output peak monitor initialized")

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
        logger.info("Starting Spotify metadata poll fallback task")
        runtime.spotify_state_poll_task = asyncio.create_task(
            _spotify_state_poll_loop(),
            name="spotify-state-poll",
        )

        runtime.player_instance.register_callbacks(_dispatch_player_state_change)
        downloader.register_callback(on_download_progress, asyncio.get_running_loop())
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
    spotify_playerctl_watch.stop()
    radio_reconnect.stop()
    silent_active_recovery.stop()
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
        apply_volume_actions=lambda *args, **kwargs: _apply_volume_actions(*args, **kwargs),
        schedule_peak_monitor_refresh=lambda reason: dsp_orchestrator.schedule_peak_monitor_refresh_after_effects_change(reason),
    )


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
        sync_peak_monitor_for_playback_state=peak_monitor_coordinator.sync_playback_state,
        sync_peak_monitor_for_spotify_state=peak_monitor_coordinator.sync_spotify_state,
        load_dsp_preset=lambda *args, **kwargs: _load_dsp_preset(*args, **kwargs),
        broadcast=lambda message: manager.broadcast(message),
        wait_for_samplerate_alignment=lambda *args, **kwargs: samplerate.wait_for_samplerate_alignment(*args, **kwargs),
        wait_for_selected_output_effective_rate=lambda *args, **kwargs: _wait_for_selected_output_effective_rate(*args, **kwargs),
        measurement_audio_graph_owned=lambda: _measurement_audio_graph_owned(),
        observe_playback_samplerate_drift=lambda: samplerate_drift.observe(),
        playback_transition_is_active=lambda: _playback_transition_is_active(),
        coordinator_target_rate=lambda *args, **kwargs: _coordinator_target_rate(*args, **kwargs),
        playback_graph_diagnosis=lambda *args, **kwargs: _playback_graph_diagnosis(*args, **kwargs),
        request_coordinated_recovery=lambda *args, **kwargs: _request_coordinated_recovery(*args, **kwargs),
        create_lifecycle_background_task=lambda coro, *, name: _create_lifecycle_background_task(coro, name=name),
        peak_monitor_restart_settle_ms=PEAK_MONITOR_RESTART_SETTLE_MS,
        sleep=lambda delay: asyncio.sleep(delay),
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
        get_audio_output_overview=lambda: get_audio_output_overview(),
        get_spotify_ui_state=lambda *args, **kwargs: get_spotify_ui_state(*args, **kwargs),
        get_player_audio_samplerate=_get_player_audio_samplerate,
        is_local_playback_active=_is_local_playback_active,
        is_spotify_playback_active=_is_spotify_playback_active,
        spotify_target_track=_spotify_target_track_from_state,
        source_rate=lambda source, track=None: playback_orchestration.configured().coordinator_source_rate(source, track),
        get_target_rate=lambda source, track=None: playback_orchestration.configured().coordinator_target_rate(source, track),
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
        pipewire_poll_interval_ms=PIPEWIRE_HANDOFF_POLL_INTERVAL_MS,
        dsp_port_timeout_ms=PLAYBACK_HANDOFF_EE_PORT_TIMEOUT_MS,
        post_start_readbacks=POST_START_GRAPH_STABILITY_READBACKS,
        output_mode_subwoofer_modes=frozenset(OUTPUT_MODE_SUBWOOFER_MODES),
        output_mode_stereo=OUTPUT_MODE_STEREO,
        playback_graph_diagnosis=lambda *args, **kwargs: _playback_graph_diagnosis(*args, **kwargs),
        transition_sample_rate_policy=lambda *args, **kwargs: playback_orchestration.configured().transition_sample_rate_policy(*args, **kwargs),
        get_dsp_snapshot=lambda: runtime.dsp_runtime.snapshot() if runtime.dsp_runtime is not None else {},
        wait_for_dsp_ports=lambda timeout_ms: _wait_for_dsp_output_ports(timeout_ms),
        mpv_source_ports_present=lambda: _mpv_source_ports_present(),
        mpv_link_repair_timeout_ms=MPV_LINK_REPAIR_TIMEOUT_MS,
        source_port_readiness_timeout_ms=RADIO_SOURCE_PORT_READINESS_TIMEOUT_MS,
        reconcile_subwoofer_links=lambda: _coordinator_reconcile_subwoofer_links_only(),
         # Let the extracted owner use the supplied low-level PipeWire
         # primitives; do not route this dependency through its public wrapper.
         repair_stereo_output_links=None,
        current_context_override=lambda: _coordinator_current_playback_context(),
        rate_change_override=lambda target_rate: _coordinator_rate_change(target_rate),
    )


playback_orchestration.configure(_make_playback_orchestration_deps())

# Bound orchestration entry points retained for application wiring and legacy
# internal callers; implementations live in playback/orchestration.py.
_coordinator_source_rate = playback_orchestration.configured().coordinator_source_rate
_coordinator_target_rate = playback_orchestration.configured().coordinator_target_rate
_sample_rate_policy_is_auto = playback_orchestration.configured().sample_rate_policy_is_auto
_transition_sample_rate_policy = playback_orchestration.configured().transition_sample_rate_policy
_coordinator_current_playback_context = playback_orchestration.configured().current_playback_context
_coordinator_rate_change = playback_orchestration.configured().coordinator_rate_change
_playback_transition_is_active = playback_orchestration.configured().transition_is_active
_coordinator_commit_context_id = playback_orchestration.configured().coordinator_commit_context_id
_recovery_context_is_valid = playback_orchestration.configured().recovery_context_is_valid
_run_coordinated_transition = playback_orchestration.configured().run_coordinated_transition
_measurement_audio_graph_owned = playback_orchestration.configured().measurement_audio_graph_owned
_request_coordinated_recovery = playback_orchestration.configured().request_coordinated_recovery
_playback_transition_context_is_current = playback_orchestration.configured().playback_transition_context_is_current
_wait_for_dsp_output_ports = playback_orchestration.configured().wait_for_dsp_output_ports
_playback_graph_diagnosis = playback_orchestration.configured().playback_graph_diagnosis
_missing_playback_graph_links = playback_orchestration.configured().missing_playback_graph_links
_measurement_session_link_loss_is_repairable = playback_orchestration.configured().measurement_session_link_loss_is_repairable
_log_playback_graph_diagnosis = playback_orchestration.configured().log_playback_graph_diagnosis
_repair_stereo_output_links_once = playback_orchestration.configured().repair_stereo_output_links_once
_coordinator_reconcile_subwoofer_links_only = playback_orchestration.configured().reconcile_subwoofer_links_only
_post_start_graph_links_are_repairable = playback_orchestration.configured().post_start_graph_links_are_repairable
_relink_missing_production_links = playback_orchestration.configured().relink_missing_production_links
_coordinator_reconcile_post_start_graph = playback_orchestration.configured().reconcile_post_start_graph
_coordinator_establish_effects_and_helper = playback_orchestration.configured().establish_effects_and_helper
_playback_graph_links_complete = playback_orchestration.configured().playback_graph_links_complete
_ensure_mpv_to_dsp_links = playback_orchestration.configured()._ensure_mpv_to_dsp_links


app = FastAPI(lifespan=lifespan)
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
    return request.url.port


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
        if parsed.port is None and trusted_port in (None, 80, 443):
            return True
        if parsed.port is None:
            # Header did not include a port; fall back to comparing
            # against the request's effective default.
            if (trusted_scheme == "https" and trusted_port == 443) or (
                trusted_scheme == "http" and trusted_port in (None, 80)
            ):
                return True
            return False
        return parsed.port == trusted_port

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
    return HTMLResponse(content=html)

@app.get("/favicon.ico")
async def favicon_root():
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")

@app.get("/apple-touch-icon.png")
async def apple_touch_icon_root():
    return FileResponse(STATIC_DIR / "apple-touch-icon.png", media_type="image/png")

@app.get("/site.webmanifest")
async def site_webmanifest_root():
    return FileResponse(STATIC_DIR / "site.webmanifest", media_type="application/manifest+json")
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
    if source not in {"local", "radio"}:
        raise HTTPException(status_code=400, detail=f"Unsupported playback source: {source}")

    active_queue_ids = [item.get("id") for item in playback_queue.queue.tracks]
    if (
        source == "local"
        and playback_queue.queue.mode == "native_mpv"
        and req.queue_track_ids
        and list(req.queue_track_ids) == active_queue_ids
        and req.track_id in active_queue_ids
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
    rate_change = _coordinator_rate_change(target_rate)
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
    if source in {"local", "radio"} and isinstance(result.target_rate, int) and result.target_rate > 0:
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
    runtime.player_instance.pause()
    new_state = runtime.player_instance.state
    _mark_player_state_authoritative(new_state)
    _mark_playback_intent_changed()
    return {
        "status": "paused" if new_state.get("paused") else "playing",
        "playback": build_playback_payload(new_state),
    }


@app.post("/api/playback/toggle")
async def toggle_playback():
    if not runtime.player_instance or not runtime.player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if _playback_transition_is_active():
        raise HTTPException(status_code=409, detail="A playback transition is in progress")
    if not _can_send_play_command():
        state = runtime.player_instance.state
        return {"status": "paused" if state.get("paused") else "playing", "playback": build_playback_payload(state)}

    state = dict(runtime.player_instance.state)
    active_track = dict(playback_state.current_track_info or {})
    if state.get("current_file") and not state.get("ended") and active_track.get("source") in {"local", "radio"}:
        was_paused = bool(state.get("paused"))
        source = str(active_track.get("source"))
        if not was_paused:
            # Same-source pause is transport only.  Resuming below remains a
            # Coordinator transition because it is a Local/Radio play action.
            runtime.player_instance.pause()
            new_state = runtime.player_instance.state
            _mark_player_state_authoritative(new_state)
            _mark_playback_intent_changed()
            return {
                "status": "playing" if not new_state.get("paused") else "paused",
                "playback": build_playback_payload(new_state),
            }
        target_rate = _coordinator_target_rate(source, active_track)
        rate_change = _coordinator_rate_change(target_rate)
        if not rate_change and target_rate is not None:
            # Same-rate resume is transport-only: the committed source, rate
            # and graph are unchanged while paused, so a full Coordinator
            # transition (gate close, quiet, effects/graph re-verification) is
            # pure latency and makes the footer flash.  Unpause directly,
            # symmetric to the pause fast path above.
            runtime.player_instance.set_pause(False)
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
        except PlaybackTransitionFailure as exc:
            raise _transition_error_http(exc) from exc
        if was_paused:
            if _sample_rate_policy_is_auto() and source in {"local", "radio"} and isinstance(result.target_rate, int) and result.target_rate > 0:
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
    request = TransitionRequest(
        operation="replay",
        source=source,
        target_rate=target_rate,
        target_url=replay_url,
        target_track=replay_track,
        should_play=True,
        rate_change=_coordinator_rate_change(target_rate),
        reload_source=True,
        detail="replay",
        **((playback_queue.queue.native_request_fields()) if source == "local" else {}),
    )
    try:
        result = await _run_coordinated_transition(request)
    except PlaybackTransitionFailure as exc:
        raise _transition_error_http(exc) from exc
    if _sample_rate_policy_is_auto() and source in {"local", "radio"} and isinstance(result.target_rate, int) and result.target_rate > 0:
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
    if playback_state.current_track_info and playback_state.current_track_info.get("source") == "radio":
        playback_state.last_radio_track_info = dict(playback_state.current_track_info)
    _mark_playback_intent_changed()
    playback_state.current_track_info = None
    radio_reconnect.reset()
    playback_queue.queue.reset()
    playback_queue.queue.reset_mpv_loop_state()
    runtime.player_instance.stop_playback()
    _mark_player_state_authoritative(runtime.player_instance.state)
    # Playback is idle: a force-rate pin left by the last source rate is stale
    # under an auto policy and would keep the live samplerate payload pinned to
    # that rate (and previously misreported mode=fixed).  Clear it so the
    # graph is unpinned and the payload reflects the auto policy.  Fixed
    # policies keep their pin (they intentionally hold the configured rate).
    try:
        status = get_samplerate_status()
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
    ensure_local_source_volume()
    # Keep local/radio output-volume changes responsive. Spotify volume uses
    # /api/spotify/volume, so this endpoint should not block on multiple
    # playerctl/Spotify status reads on slow boards.
    await manager.broadcast({"type": "playback", "data": build_playback_payload(runtime.player_instance.state)})
    return {
        "volume": volume_result["volume"],
        **({"loudnessVolumeDb": volume_result["loudnessVolumeDb"]}
           if volume_result.get("loudness_enabled") else {}),
    }

@app.post("/api/playback/next")
async def next_playback():
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
    runtime.player_instance.seek(pos)
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
        if track.get("source") in ("radio", "local") and state.get("current_file"):
            state["stream_info"] = normalize_stream_info(
                await _read_status_player_detail(runtime.player_instance.get_stream_audio_info, {})
            )
        else:
            state["stream_info"] = None
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


@app.post("/api/system/update")
async def system_update():
    service_name = _configured_service_name()
    result = await _run_update_operation(_UPDATE_APPLY_TIMEOUT_SECONDS, "--defer-restart")
    ok = result["returncode"] == 0
    stdout = result.get("stdout", "")
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


@app.post("/api/system/restore")
async def system_restore():
    """Restore the checkout to origin/main and return to a clean public release.

    This is an explicit repair action, not a normal update. It saves local
    source changes as a patch file in backups/, then resets the working tree
    to origin/main and restarts the service.

    User data, music, config, and runtime cache files are not affected.
    """
    service_name = _configured_service_name()
    result = await _run_update_operation(_UPDATE_APPLY_TIMEOUT_SECONDS, "--restore", "--defer-restart")
    ok = result["returncode"] == 0
    if ok:
        asyncio.create_task(_restart_fxroute_service_after_response(service_name))
    return {
        "ok": ok,
        "installed_version": _read_version_file(),
        "restart_scheduled": ok,
        "service_name": service_name,
        **result,
    }





@app.get("/api/audio/samplerate")
async def audio_samplerate_status():
    status = await asyncio.to_thread(get_samplerate_status)
    logger.info(
        "audio_samplerate_status entry: footer_owner=%s active_rate=%s sink_state=%s",
        playback_state.current_footer_owner,
        status.get("active_rate"),
        (status.get("relevant_sink") or {}).get("state"),
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
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid JSON body, expected {"mode": "auto"|"fixed", "rate": <number?>}')

    try:
        await _transition_sample_rate_policy(policy, detail="api-audio-samplerate-policy")
        return get_samplerate_status()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        raise HTTPException(status_code=400, detail=str(exc))
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

        if runtime.dsp_runtime is None:
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
            await dsp_orchestrator.refresh_peak_monitor_after_effects_change("audio-output-mode-switch")
            return result

        previous_overview = get_audio_output_overview()
        try:
            await runtime.dsp_runtime.guarded_rebuild(
                target["overview"],
                guard_db=mode_transition_guard(target["overview"]),
                apply_candidate=lambda: None,
                apply_previous=lambda: None,
                settle_seconds=0.0,
            )
            result = persist_audio_output_mode(target["config"])
        except Exception:
            try:
                await runtime.dsp_runtime.sync(previous_overview)
            except Exception:
                logger.exception("Failed to restore native DSP after output-mode transition failure")
            raise

        result = with_subwoofer_derived_delays(result)
        if runtime.dsp_runtime is not None:
            result["output_mode"] = {
                **(result.get("output_mode") or {}),
                "runtime": runtime.dsp_runtime.snapshot(),
            }
        await dsp_orchestrator.refresh_peak_monitor_after_effects_change("audio-output-mode-switch")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
            runtime.player_instance.stop_playback()
            await manager.broadcast({"type": "playback", "data": build_playback_payload(runtime.player_instance.state)})
            released = await _wait_for_pipewire_mpv_release()
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
        result = set_audio_source_selection(mode, input_key)
        result = await external_input.sync(result)
        result = await bluetooth_input.sync(result)
        if result.get("mode") in {SOURCE_MODE_EXTERNAL_INPUT, SOURCE_MODE_BLUETOOTH_INPUT}:
            await _pause_all_app_playback_for_external_input()
        await peak_monitor_coordinator.sync_source_mode_state(result)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
    acquired first (so the Direct volume-ownership transfer serializes
    against volume writes), then the DSP mutation lock.  Callers
    that already hold both locks pass ``_locks_held=True``.
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
    target = volume_contract.target_for(current=start, preset=preset_name)
    actions = volume_contract.plan_transition(start, target)
    pre, post = volume_contract.partition_actions(actions, ("set_preset",))
    # The target DSP must be live before restoring a master level that was
    # previously owned by Loudness.  Keep that write on the post-sync side of
    # the preset transition, but prepare volumeDb while Direct still owns the
    # output.
    deferred_master = [action for action in post if action.op == "set_master"]
    post = [action for action in post if action.op != "set_master"]
    try:
        await _apply_volume_actions(pre)
        await _drain_worker(
            manager.load_preset,
            preset_name,
            convolver_sample_rate_hz=convolver_sample_rate_hz,
        )
        await dsp_orchestrator.sync_runtime(reason="native-dsp-preset-load",
                                            _rate_lock_held=_rate_lock_held)
        await _apply_volume_actions(post)
        await _apply_volume_actions(deferred_master)
    except Exception:
        try:
            if (manager.get_active_preset() or "") != start.preset:
                try:
                    await _drain_worker(manager.load_preset, start.preset)
                except Exception:
                    logger.exception("Failed to reload previous preset after preset load failure")
                    if hasattr(manager, "active_preset"):
                        manager.active_preset = start.preset
            await _restore_volume_state(manager, start)
            await dsp_orchestrator.sync_runtime(reason="native-dsp-preset-load-rollback",
                                                _rate_lock_held=_rate_lock_held)
        except Exception:
            logger.exception("Failed to restore volume state after preset load failure")
        raise


async def _transfer_volume_ownership_for_preset(manager, preset_name: str) -> None:
    """Move the canonical attenuation across the Direct bypass boundary.

    The planner owns the order: raise the master only after Loudness is in
    the live path, and lower it before Loudness leaves the path.
    """
    start = await _volume_state_for_manager(manager)
    target = volume_contract.target_for(current=start, preset=preset_name)
    pre, _post = volume_contract.partition_actions(
        volume_contract.plan_transition(start, target), ("set_preset",)
    )
    await _apply_volume_actions(pre)



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
    return await asyncio.to_thread(manager.status)


@app.post("/api/music-libraries/manual")
async def add_manual_music_library(request: Request):
    manager = runtime.music_library.manager
    if manager is None:
        raise HTTPException(status_code=503, detail="Music libraries are not initialized")
    try:
        body = await request.json()
        entry = manager.add_manual_url(str(body.get("url") or ""))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"entry": entry, **await asyncio.to_thread(manager.status)}


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
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if root == scanner.music_root:
            return manager.status()
        scanner.cancel_refresh()
        active_refreshes = [task for task in runtime.library_refresh_tasks if not task.done()]
        if active_refreshes:
            await asyncio.gather(*active_refreshes, return_exceptions=True)
        if runtime.player_instance is not None and runtime.player_instance._running:
            _mark_playback_intent_changed()
            runtime.player_instance.stop_playback()
            playback_state.current_track_info = None
            playback_state.last_track_info = None
        playback_queue.queue.reset()
        runtime.music_library.scanner = _library_scanner_for(root, library_id)
        runtime.music_library.scanner.prepare_scan_status()
        runtime.library_scan_task = _create_library_refresh_task(runtime.music_library.scanner, name="selected-library-scan")
        return manager.status()


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
        logger.error(f"Download error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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


# ---------------------------------------------------------------------------
# Spotify (playerctl / MPRIS)
# ---------------------------------------------------------------------------

@app.get("/api/spotify/status")
async def api_spotify_status():
    data = await get_spotify_ui_state()
    playback_state.latest_spotify_state = data
    await peak_monitor_coordinator.sync_spotify_state(data)
    return data


@app.post("/api/spotify/play")
async def api_spotify_play():
    target_rate = _coordinator_target_rate("spotify")
    request = TransitionRequest(
        operation="spotify-play",
        source="spotify",
        target_rate=target_rate,
        should_play=True,
        rate_change=_coordinator_rate_change(target_rate),
        reload_source=True,
        detail="api-spotify-play",
    )
    try:
        result = await _run_coordinated_transition(request)
    except PlaybackTransitionFailure as exc:
        raise _transition_error_http(exc) from exc
    # After the coordinator commit the Spotify source is already the
    # committed playback context; the subsequent state read is
    # telemetry/UI refresh and not part of the ownership boundary.
    # Publish synchronously before any further await so the ended waiter
    # never sees a window with a new token and an old footer.
    playback_state.current_footer_owner = "spotify"
    _publish_playback_context_commit(getattr(result, "transition_id", None))
    playback_state.latest_spotify_state = await get_spotify_ui_state()
    return await broadcast_spotify_state(playback_state.latest_spotify_state)


@app.post("/api/spotify/pause")
async def api_spotify_pause():
    # Spotify pause is an MPRIS transport command, not a source handoff.
    data = await spotify_pause()
    return await broadcast_spotify_state(data)


@app.post("/api/spotify/toggle")
async def api_spotify_toggle():
    sd = await get_spotify_ui_state()
    if sd.get("status") == "Playing":
        # Toggling an already-playing Spotify source is transport-only.  In
        # particular it must not quiet MPV or clear the local queue/context.
        data = await spotify_pause()
        return await broadcast_spotify_state(data)

    target_rate = _coordinator_target_rate("spotify")
    request = TransitionRequest(
        operation="spotify-toggle",
        source="spotify",
        target_rate=target_rate,
        should_play=True,
        rate_change=_coordinator_rate_change(target_rate),
        reload_source=True,
        detail="api-spotify-toggle",
    )
    try:
        result = await _run_coordinated_transition(request)
    except PlaybackTransitionFailure as exc:
        raise _transition_error_http(exc) from exc
    # Same ownership contract as api_spotify_play: footer and token are
    # published synchronously after the commit, before the Spotify state
    # is read or broadcast.
    playback_state.current_footer_owner = "spotify"
    _publish_playback_context_commit(getattr(result, "transition_id", None))
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


@app.post("/api/spotify/volume")
async def api_spotify_volume(request: Request):
    body = await request.json()
    volume = float(body.get("volume", 100))
    try:
        volume_result = await _set_canonical_output_volume(volume)
    except SystemVolumeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to set output volume: {exc}")
    ensure_local_source_volume()
    data = await broadcast_spotify_state()
    data["volume"] = volume_result["volume"]
    if volume_result.get("loudness_enabled"):
        data["loudnessVolumeDb"] = volume_result["loudnessVolumeDb"]
    return data


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    # Init is queued before the client is marked ready, so it is always the
    # first payload delivered by the per-client send worker.
    await manager.send_to_client(websocket, json.dumps({"type": "init", "data": {"player": {"state": build_playback_payload()}, "spotify": await get_spotify_ui_state()}}))
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
