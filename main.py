# SPDX-License-Identifier: AGPL-3.0-only

"""Main FastAPI application for FXRoute."""

import copy
import json
import logging
import os
import re
import shutil
import time
import asyncio
import hashlib
import math
import statistics
import random
import subprocess
import tempfile
import wave
import zipfile
import numpy as np
import samplerate_orchestration
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Mapping, Optional
from urllib.parse import quote, unquote, urlparse
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from mutagen import File as MutagenFile
from starlette.background import BackgroundTask

from config import get_settings
from radio_metadata import RadioMetadataService

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
COVER_CACHE_DIR = BASE_DIR / "media" / "cache" / "covers"
TOP40_COVER_IMAGE = STATIC_DIR / "Top40.png"
UPDATE_SCRIPT = BASE_DIR / "scripts" / "update_fxroute.sh"

# Cooldown to prevent rapid mpv IPC flooding (ms)
PLAY_COMMAND_COOLDOWN_MS = 400
LOCAL_TRACK_SWITCH_SETTLE_MS = 260
SOURCE_HANDOFF_SETTLE_MS = 260
PIPEWIRE_HANDOFF_RELEASE_TIMEOUT_MS = 1800
PIPEWIRE_HANDOFF_POLL_INTERVAL_MS = 50
SPOTIFY_SINK_INPUT_RATE_TIMEOUT_MS = 1800
SPOTIFY_SINK_INPUT_RATE_STABILITY_POLLS = 2
PEAK_MONITOR_INACTIVE_GRACE_MS = 450
PEAK_MONITOR_RESTART_SETTLE_MS = 320
PEAK_MONITOR_RATE_MATCH_TIMEOUT_MS = 900
RADIO_EXPECTED_SAMPLE_RATE_HZ = 44100
RADIO_POST_LOAD_RATE_TIMEOUT_MS = 3000
RADIO_POST_LOAD_RATE_STABILITY_POLLS = 3
# Bounded readback wait for the EasyEffects output ports after a rate switch
# or a missing-graph repair. No fixed sleeps: the handoff polls pw-link until
# ee_soe_output_level:output_FL/FR are exposed, then starts/syncs the helper.
PLAYBACK_HANDOFF_EE_PORT_TIMEOUT_MS = 5000
# A post-source-start graph repair is deliberately a short, deterministic
# readback window.  It is not a second watcher or a general graph recovery.
POST_START_GRAPH_STABILITY_READBACKS = 2
SPOTIFY_PREARM_SAMPLE_RATE_HZ = 44100
RADIO_RECONNECT_DELAY_SECONDS = 2.0
RADIO_RECONNECT_MAX_ATTEMPTS = 5
SPOTIFY_STATE_POLL_INTERVAL_SECONDS = 2.0
SPOTIFY_STATE_IDLE_POLL_INTERVAL_SECONDS = 5.0
SPOTIFY_STATE_REFRESH_DEBOUNCE_SECONDS = 0.20
MEASUREMENT_WINDOW_TTL_SECONDS = 30.0
SILENT_ACTIVE_SETTLE_SECONDS = 8.0
SILENT_ACTIVE_FLOOR_DB = -58.0
SILENT_ACTIVE_RECHECK_SECONDS = 2.5

# Track last play command time to debounce rapid requests
_last_play_command_time = 0.0


def _path_within_root(path: Path, root: Path) -> bool:
    """Thin wrapper: path containment check lives in library (REFACTOR-007)."""
    return path_within_root(path, root)


def _can_send_play_command():
    """Debounce rapid play/pause/seek commands to prevent mpv IPC overload."""
    global _last_play_command_time
    now = time.monotonic()
    if now - _last_play_command_time < PLAY_COMMAND_COOLDOWN_MS / 1000:
        return False
    _last_play_command_time = now
    return True


def _cleanup_temp_file(path: Path):
    path.unlink(missing_ok=True)


def _read_version_file() -> str:
    """Thin wrapper: VERSION reading lives in install_info (REFACTOR-009)."""
    return install_info.read_version_file()


def _read_build_id() -> str:
    """Thin wrapper: build-id resolution lives in install_info (REFACTOR-009)."""
    return install_info.read_build_id()


def _read_install_config() -> dict:
    """Thin wrapper: install-config parsing lives in install_info (REFACTOR-009)."""
    return install_info.read_install_config()


def _configured_service_name() -> str:
    """Thin wrapper: service-name resolution lives in install_info (REFACTOR-009)."""
    return install_info.configured_service_name()


async def _run_update_script(*args: str) -> dict:
    if not UPDATE_SCRIPT.exists():
        raise HTTPException(status_code=500, detail=f"Update script missing: {UPDATE_SCRIPT}")
    proc = await asyncio.create_subprocess_exec(
        str(UPDATE_SCRIPT),
        *args,
        cwd=str(BASE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return {
        "returncode": proc.returncode,
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
    }


async def _restart_fxroute_service_after_response(service_name: str) -> None:
    await asyncio.sleep(0.8)
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "restart",
            f"{service_name}.service",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception as exc:
        logger.warning("Deferred FXRoute service restart failed: %s", exc)


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










async def _wait_for_samplerate_alignment(expected_rate: Optional[int], timeout_ms: int = PEAK_MONITOR_RATE_MATCH_TIMEOUT_MS) -> bool:
    if not expected_rate or expected_rate <= 0:
        return False
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while time.monotonic() <= deadline:
        try:
            samplerate_status = get_samplerate_status()
        except Exception:
            samplerate_status = {}
        sink_rate = samplerate_status.get("active_rate")
        if isinstance(sink_rate, int) and sink_rate == expected_rate:
            return True
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
    return False


# ── Centralized Sink Suspend/Resume ──
_last_sink_suspend_at: float = 0.0
_last_sink_suspend_reason: str = ""
_SINK_SUSPEND_COOLDOWN_SECONDS: float = 3.0

async def _suspend_resume_playback_sink(*, reason: str = "", output_key: str | None = None, force: bool = False) -> bool:
    """Central sink suspend/resume to force PipeWire rate re-negotiation.

    Args:
        reason: diagnostic label for logging
        output_key: pactl sink name; resolved from overview if None
        force: bypass cooldown

    Returns True if suspend/resume completed.
    """
    global _last_sink_suspend_at, _last_sink_suspend_reason
    now = time.monotonic()
    elapsed = now - _last_sink_suspend_at
    if not force and _last_sink_suspend_at > 0 and elapsed < _SINK_SUSPEND_COOLDOWN_SECONDS:
        logger.warning(
            "Sink suspend/resume SKIPPED (cooldown %.1fs): reason=%s last_reason=%s",
            elapsed, reason, _last_sink_suspend_reason,
        )
        return False
    if output_key is None:
        overview = get_audio_output_overview()
        output_mode = overview.get("output_mode") or {}
        output_key = str(output_mode.get("effective_output_key") or "").strip()
    if not output_key:
        logger.warning("Sink suspend/resume SKIPPED: no output_key (reason=%s)", reason)
        return False
    logger.info("Sink suspend/resume START: reason=%s output_key=%s", reason, output_key)
    try:
        _pulse_suspend_sink_for_samplerate(output_key, reason)
    except Exception as exc:
        logger.error("Sink suspend/resume FAILED: reason=%s output_key=%s error=%s", reason, output_key, exc)
        return False
    _last_sink_suspend_at = time.monotonic()
    _last_sink_suspend_reason = reason
    logger.info("Sink suspend/resume DONE: reason=%s output_key=%s", reason, output_key)
    return True



def _set_pipewire_force_rate(rate: int) -> None:
    completed = subprocess.run(
        ["pw-metadata", "-n", "settings", "0", "clock.force-rate", str(rate)],
        capture_output=True,
        text=True,
        check=False,
        timeout=1.5,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or f"pw-metadata clock.force-rate {rate} failed")






def _get_current_pipewire_force_rate() -> Optional[int]:
    try:
        status = get_samplerate_status()
    except Exception:
        return None
    force_rate = status.get("force_rate") if isinstance(status, dict) else None
    return force_rate if isinstance(force_rate, int) and force_rate > 0 else 0


def _measurement_session_blocks_playback_rate(expected_rate: Optional[int]) -> bool:
    return bool(
        measurement_sr_session is not None
        and measurement_sr_session.active
        and isinstance(expected_rate, int)
        and expected_rate != measurement_sr_session.measurement_rate
    )


async def _ensure_playback_samplerate_force(
    expected_rate: Optional[int],
    reason: str,
    *,
    allow_measurement_session: bool = False,
    policy: samplerate_orchestration.PlaybackRateReconcilePolicy = samplerate_orchestration.DEFAULT_POLICY,
) -> bool:
    global playback_samplerate_force_rate
    if not isinstance(expected_rate, int) or expected_rate <= 0:
        return False
    if not allow_measurement_session and _measurement_session_blocks_playback_rate(expected_rate):
        logger.info(
            "Playback samplerate repair deferred to active measurement session: "
            "reason=%s playback_rate=%s measurement_rate=%s",
            reason,
            expected_rate,
            measurement_sr_session.measurement_rate,
        )
        return False

    initial_status: dict = {}
    force_rate_written = False
    pulse_attempted = False
    pulse_succeeded = False

    def read_status() -> dict:
        nonlocal initial_status
        try:
            initial_status = get_samplerate_status()
        except Exception:
            initial_status = {}
        return initial_status

    def write_force_rate(rate: int) -> None:
        nonlocal force_rate_written
        global playback_samplerate_force_rate
        _set_pipewire_force_rate(rate)
        playback_samplerate_force_rate = rate
        force_rate_written = True
        logger.info(
            "Playback samplerate force-rate applied: reason=%s expected_rate=%s active_rate=%s previous_force_rate=%s",
            reason,
            expected_rate,
            initial_status.get("active_rate"),
            initial_status.get("force_rate"),
        )

    async def wait_for_alignment(rate: int, timeout_ms: int) -> bool:
        return await _wait_for_samplerate_alignment(rate, timeout_ms=timeout_ms)

    async def pulse_sink(pulse_reason: str) -> bool:
        nonlocal pulse_attempted, pulse_succeeded
        pulse_attempted = True
        pulse_succeeded = await _suspend_resume_playback_sink(
            reason=pulse_reason, force=True,
        )
        return pulse_succeeded

    aligned = await samplerate_orchestration.reconcile_playback_samplerate(
        expected_rate=expected_rate,
        reason=reason,
        policy=policy,
        read_status=read_status,
        write_force_rate=write_force_rate,
        wait_for_alignment=wait_for_alignment,
        pulse_sink=pulse_sink,
    )

    initial_active_rate = initial_status.get("active_rate")
    initial_force_rate = initial_status.get("force_rate")
    if force_rate_written:
        playback_samplerate_force_rate = expected_rate
    elif (
        initial_active_rate == expected_rate
        and initial_force_rate == expected_rate
    ):
        playback_samplerate_force_rate = expected_rate

    if policy is samplerate_orchestration.DEFAULT_POLICY and not aligned:
        if isinstance(initial_active_rate, int) and initial_active_rate != expected_rate:
            logger.info(
                "Radio samplerate sink suspend/resume SKIPPED: reason=%s "
                "(only for radio-start/restart paths)",
                reason,
            )
    elif policy is samplerate_orchestration.RADIO_POLICY and pulse_attempted:
        if aligned:
            logger.info(
                "Radio samplerate sink suspend/resume succeeded: reason=%s expected_rate=%s",
                reason, expected_rate,
            )
        else:
            logger.warning(
                "Radio samplerate sink suspend/resume did not change rate: reason=%s expected_rate=%s",
                reason, expected_rate,
            )
    elif policy is samplerate_orchestration.STATUS_DRIFT_REPAIR_POLICY and not aligned:
        try:
            post_attempt_status = get_samplerate_status()
        except Exception:
            post_attempt_status = {}
        logger.warning(
            "Playback samplerate drift repair aborted: sink did not align "
            "source=%s expected_rate=%s active_rate=%s suspended=%s",
            reason.removeprefix("status-drift-repair:") or "unknown",
            expected_rate,
            post_attempt_status.get("active_rate"),
            pulse_succeeded,
        )
    return aligned






def _is_local_playback_active(state: dict | None) -> bool:
    return playback_state.is_local_playback_active(state)


def _is_spotify_playback_active(state: dict | None) -> bool:
    return playback_state.is_spotify_playback_active(state)


def _is_measurement_window_open() -> bool:
    if last_measurement_window_seen_at <= 0:
        return False
    return (time.monotonic() - last_measurement_window_seen_at) <= MEASUREMENT_WINDOW_TTL_SECONDS


def _build_power_state_payload() -> dict:
    local_state = player_instance.state if player_instance else {}
    spotify_state = latest_spotify_state or {}
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
    track = current_track_info or state.get("current_track") or {}
    source = (track or {}).get("source")
    if source not in {"local", "radio"}:
        return False
    return bool(
        state.get("current_file")
        or state.get("playing")
        or state.get("paused")
        or state.get("ended")
    )


def _get_authoritative_footer_owner(playback_state: dict | None = None, spotify_state: dict | None = None) -> str:
    global current_footer_owner, latest_spotify_state, player_instance
    playback_state = playback_state or (player_instance.state if player_instance else {})
    spotify_state = spotify_state or latest_spotify_state or {}

    # A loaded/paused MPV file is only fallback context.  First resolve the
    # sources that are actually producing audio; otherwise a paused Local
    # track would mask a currently playing Spotify client (and vice versa).
    local_active = _is_local_playback_active(playback_state)
    spotify_active = _is_spotify_playback_active(spotify_state)
    if spotify_active and not local_active:
        current_footer_owner = "spotify"
        return current_footer_owner
    if local_active and not spotify_active:
        current_footer_owner = "local"
        return current_footer_owner

    if local_active and spotify_active:
        # This is an inconsistent dual-active readback.  Keep the already
        # committed owner when possible; it is deterministic and avoids a UI
        # flip while the two source states converge.
        if current_footer_owner in {"local", "spotify"}:
            return current_footer_owner
        current_footer_owner = "spotify"
        return current_footer_owner

    # Neither source is active.  The committed owner is the first fallback;
    # only an unowned loaded MPV context may establish a local fallback.
    if current_footer_owner in {"local", "spotify"}:
        return current_footer_owner
    if _has_local_footer_context(playback_state):
        current_footer_owner = "local"
        return current_footer_owner
    return current_footer_owner or "local"




def _dedupe_archive_name(name: str, used_names: set[str]) -> str:
    """Thin wrapper: archive name deduplication lives in zip_album (REFACTOR-008)."""
    return zip_album.dedupe_archive_name(name, used_names)

from models import (
    DeleteFolderRequest,
    DeleteTracksRequest,
    DownloadTracksRequest,
    PlaylistSaveRequest,
    PlayRequest,
)
from player import get_player, MPVNotInstalledError, MPVError, normalize_stream_info
from radio_api import _station_api_payload, router as radio_api_router
from stations import get_stations
from playlists import delete_playlist, get_playlists, save_playlist
import playlist_io
import sink_inputs
import playback_state
import samplerate
from library import (
    AUDIO_EXTENSIONS,
    LibraryScanner,
    cleanup_track_parent_folder,
    folder_has_audio_files,
    is_cleanup_only_file,
    is_removable_artwork_file,
    is_removable_metadata_sidecar,
    path_within_root,
)
from downloader import Downloader
from easyeffects import EasyEffectsManager
try:
    from hardware_controller import HardwareController
except ImportError:
    HardwareController = None
from measurement import (
    MEASUREMENT_DEFAULT_SAMPLE_RATE,
    MeasurementStore,
    measurement_setup_settings_from_payload,
    normalize_measurement_optional_input_channel,
    score_sub_alignment_candidates,
)
from peak_monitor import EasyEffectsPeakMonitor
from subwoofer_runtime import Subwoofer21Runtime, SubwooferRuntimeConfig, DEFAULT_SAMPLE_RATE
from playback_transition import (
    PlaybackTransitionCoordinator,
    PlaybackTransitionFailure,
    TransitionRequest,
    TransitionRuntime,
)


def _is_removable_artwork_file(path: Path) -> bool:
    """Thin wrapper: artwork detection lives in library (REFACTOR-007)."""
    return is_removable_artwork_file(path)


def _is_removable_metadata_sidecar(path: Path) -> bool:
    """Thin wrapper: sidecar detection lives in library (REFACTOR-007)."""
    return is_removable_metadata_sidecar(path)


def _is_cleanup_only_file(path: Path) -> bool:
    """Thin wrapper: cleanup-only classification lives in library (REFACTOR-007)."""
    return is_cleanup_only_file(path)


def _folder_has_audio_files(folder: Path) -> bool:
    """Thin wrapper: audio presence check lives in library (REFACTOR-007)."""
    return folder_has_audio_files(folder)


def _cleanup_track_parent_folder(folder: Path, music_root: Path, protected_folders: Optional[set[Path]] = None) -> dict:
    """Thin wrapper: track-parent cleanup lives in library (REFACTOR-007)."""
    return cleanup_track_parent_folder(folder, music_root, protected_folders=protected_folders)


def _resolve_library_folder(folder: str, music_root: Path) -> Path:
    requested = Path(str(folder or "").strip().lstrip("/"))
    if not str(requested):
        raise HTTPException(status_code=400, detail="folder is required")
    if requested.is_absolute() or ".." in requested.parts:
        raise HTTPException(status_code=400, detail="Invalid folder path")
    folder_path = (music_root / requested).resolve()
    if folder_path == music_root.resolve() or not _path_within_root(folder_path, music_root):
        raise HTTPException(status_code=403, detail="Folder path outside music root")
    if not folder_path.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")
    return folder_path
from samplerate import (
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
    persist_audio_output_mode,
    prepare_audio_output_mode,
    set_audio_output_mode,
    set_audio_output_selection,
    set_audio_source_selection,
    set_bluetooth_receiver_enabled,
)
from spotify import (
    _stop_process,
    playerctl_available,
    spotify_installed,
    get_status as spotify_get_status,
    play as spotify_play,
    pause as spotify_pause,
    toggle as spotify_toggle,
    next_track as spotify_next,
    previous as spotify_previous,
    shuffle_toggle as spotify_shuffle_toggle,
    loop_cycle as spotify_loop_cycle,
    seek_to as spotify_seek_to,
    set_volume as spotify_set_volume,
)
from system_volume import SystemVolumeError, get_output_volume, set_output_volume

logger = logging.getLogger(__name__)

import zip_album
from zip_album import UPLOAD_AUDIO_EXTENSIONS, PLAYLIST_FILE_EXTENSIONS
import install_info
import effects_extras


class MeasurementSampleRateSession:
    """Own the PipeWire force-rate for one measurement-window session."""

    def __init__(self) -> None:
        self.active = False
        self.measurement_rate = 48_000
        self.original_force_rate = 0
        self.active_manual_job_ids: set[str] = set()
        self.active_auto_sub_job_id: str | None = None
        self.active_spl_job_ids: set[str] = set()
        self.close_requested = False
        self.deferred_release_pending = False
        self.generation = 0
        self.lock = asyncio.Lock()
        self._playback_captured = False
        self._rate_changed = False

    async def _start_locked(self, measurement_rate: int) -> int:
        if self.active:
            return self.generation
        # Reset old snapshots so a new session always starts fresh.
        global _playback_state_before_measurement, _radio_state_before_measurement
        _playback_state_before_measurement = None
        _radio_state_before_measurement = None
        self.measurement_rate = int(measurement_rate)
        self._playback_captured = False
        self._rate_changed = False
        try:
            status = get_samplerate_status()
            self.original_force_rate = int(status.get("force_rate") or 0)
        except Exception as exc:
            logger.warning("Measurement sample-rate session could not read force-rate: %s", exc)
            self.original_force_rate = 0
        try:
            context = await _coordinator_current_playback_context()
            # Capture playback state from the same read-only live context that
            # the guarded measurement-entry transition will pause.  This is
            # especially important for Spotify, where current_track_info and
            # MPV are intentionally unrelated transport state.
            _capture_playback_state_before_measurement(context)
            transition_result = await _run_coordinated_transition(TransitionRequest(
                operation="measurement-entry",
                source=str(context.get("source") or "local"),
                target_rate=self.measurement_rate,
                target_url=context.get("target_url"),
                target_track=dict(context.get("target_track") or {}),
                should_play=False,
                rate_change=self.original_force_rate != self.measurement_rate,
                reload_source=False,
                detail="measurement-entry",
            ))
            if not transition_result.committed:
                raise RuntimeError("measurement entry was not committed")
            self._rate_changed = self.original_force_rate != self.measurement_rate
        except Exception as exc:
            logger.error(
                "Measurement sample-rate session could not establish its guarded entry at %s Hz: %s",
                self.measurement_rate,
                exc,
            )
            self._playback_captured = False
            _playback_state_before_measurement = None
            _radio_state_before_measurement = None
            self.original_force_rate = 0
            raise RuntimeError(
                f"Could not establish guarded measurement entry at {self.measurement_rate} Hz"
            ) from exc
        self.active = True
        self.close_requested = False
        self.deferred_release_pending = False
        logger.info(
            "Measurement sample-rate session started: generation=%s measurement_rate=%s original_force_rate=%s",
            self.generation,
            self.measurement_rate,
            self.original_force_rate,
        )
        return self.generation

    async def start(self, measurement_rate: int) -> int:
        async with self.lock:
            return await self._start_locked(measurement_rate)

    async def register_manual_job(self, job_id: str) -> int:
        async with self.lock:
            if not self.active:
                logger.info("Measurement sample-rate session start requested: caller=manual-sweep job_id=%s", job_id)
                await self._start_locked(_resolve_measurement_start_sample_rate())
            self.active_manual_job_ids.add(job_id)
            return self.generation

    async def replace_manual_job(self, old_job_id: str, job_id: str) -> None:
        async with self.lock:
            self.active_manual_job_ids.discard(old_job_id)
            self.active_manual_job_ids.add(job_id)

    async def unregister_manual_job(self, job_id: str) -> None:
        async with self.lock:
            self.active_manual_job_ids.discard(job_id)
            await self._check_release()

    async def register_auto_sub(self, job_id: str) -> int:
        async with self.lock:
            if not self.active:
                logger.info("Measurement sample-rate session start requested: caller=auto-sub job_id=%s", job_id)
                await self._start_locked(_resolve_measurement_start_sample_rate())
            self.active_auto_sub_job_id = job_id
            return self.generation

    async def register_spl_job(self, job_id: str) -> int:
        async with self.lock:
            if not self.active:
                logger.info("Measurement sample-rate session start requested: caller=spl-meter job_id=%s", job_id)
                await self._start_locked(_resolve_measurement_start_sample_rate())
            self.active_spl_job_ids.add(job_id)
            return self.generation

    async def unregister_spl_job(self, job_id: str) -> None:
        async with self.lock:
            self.active_spl_job_ids.discard(job_id)
            await self._check_release()

    async def unregister_auto_sub(self, job_id: str) -> None:
        async with self.lock:
            if self.active_auto_sub_job_id == job_id:
                self.active_auto_sub_job_id = None
            await self._check_release()

    async def request_open(self) -> None:
        """Record a heartbeat without changing the audio sample rate."""
        async with self.lock:
            logger.info(
                "Measurement sample-rate session heartbeat: caller=measurement-window-open active=%s action=state-only",
                self.active,
            )
            if self.active:
                self.close_requested = False
                self.deferred_release_pending = False

    async def request_close(self) -> None:
        async with self.lock:
            self.close_requested = True
            released = await self._check_release()
            if not released:
                self.deferred_release_pending = True

    async def _check_release(self) -> bool:
        if not self.active or not self.close_requested:
            return False
        if self.active_auto_sub_job_id is not None or self.active_manual_job_ids or self.active_spl_job_ids:
            return False
        await self._release()
        return True

    async def _release(self) -> None:
        global playback_stream_stale_after_measurement, radio_stream_stale_after_measurement
        global _playback_state_before_measurement, _radio_state_before_measurement

        restore_value = self.original_force_rate if self.original_force_rate > 0 else 0
        captured_playback_snapshot = _playback_state_before_measurement
        snapshot_is_current = await _measurement_restore_snapshot_matches_current_intent(
            captured_playback_snapshot
        ) if captured_playback_snapshot else False
        if captured_playback_snapshot and not snapshot_is_current:
            logger.info(
                "Measurement playback snapshot discarded: current playback intent "
                "no longer matches the captured track; no old source will be resurrected"
            )
            _playback_state_before_measurement = None
            _radio_state_before_measurement = None
        playback_snapshot = captured_playback_snapshot if snapshot_is_current else {}
        playback_source = playback_snapshot.get("source") or (current_track_info or {}).get("source")
        playback_target_rate = playback_snapshot.get("expected_rate")
        if not isinstance(playback_target_rate, int) or playback_target_rate <= 0:
            playback_target_rate = None
        target_rate = playback_target_rate or restore_value
        force_rate_owned = True
        coordinator_attempted = False
        playback_restore_via_coordinator = bool(
            playback_source in {"local", "radio", "spotify"}
            and playback_target_rate
            and snapshot_is_current
        )
        if playback_restore_via_coordinator:
            coordinator_attempted = True
            track = dict(
                (playback_snapshot.get("track_info") or {})
                if playback_source == "spotify"
                else (current_track_info or {})
            )
            track.update({
                "source": playback_source,
                "url": playback_snapshot.get("url") or playback_snapshot.get("path"),
                "path": playback_snapshot.get("path") or playback_snapshot.get("url"),
                "id": playback_snapshot.get("id"),
                "title": playback_snapshot.get("title"),
                "sample_rate_hz": playback_target_rate,
            })
            try:
                restore_result = await playback_transition_coordinator.restore_measurement(
                    source=playback_source,
                    target_rate=playback_target_rate,
                    target_url=track.get("url"),
                    target_track=track,
                    should_play=bool(playback_snapshot.get("was_playing")),
                    rate_change=self._rate_changed,
                    reload_source=self._rate_changed,
                    restore_position=(
                        playback_snapshot.get("position")
                        if playback_source == "local"
                        else None
                    ),
                    restore_intent=playback_snapshot,
                )
                if not restore_result.committed:
                    logger.info(
                        "Measurement restore was skipped because its playback intent changed "
                        "inside the Coordinator contract"
                    )
                    playback_stream_stale_after_measurement = True
                    if playback_source == "radio":
                        radio_stream_stale_after_measurement = True
                    coordinator_attempted = False
                    playback_restore_via_coordinator = False
                    playback_snapshot = {}
                    playback_source = None
                    playback_target_rate = None
                    target_rate = restore_value
                else:
                    self._rate_changed = False
                    playback_stream_stale_after_measurement = False
                    radio_stream_stale_after_measurement = False
                    _playback_state_before_measurement = None
                    _radio_state_before_measurement = None
                    logger.info(
                        "Measurement restore committed through PlaybackTransitionCoordinator: source=%s target_rate=%s",
                        playback_source,
                        playback_target_rate,
                    )
            except Exception as exc:
                logger.warning("Measurement restore through coordinator failed; retaining safe state: %s", exc)
                playback_stream_stale_after_measurement = True
                if playback_source == "radio":
                    radio_stream_stale_after_measurement = True
        try:
            # Active playback restoration is exclusively coordinator-owned.  A
            # failed or unavailable coordinator must leave the playback gate
            # in its safe state instead of falling through to a second direct
            # force-rate/helper restore path.
            if self._rate_changed and not coordinator_attempted and playback_restore_via_coordinator:
                force_rate_owned = False
                logger.warning(
                    "Measurement sample-rate session playback restore requires the PlaybackTransitionCoordinator; "
                    "direct restore is intentionally suppressed: source=%s target_rate=%s",
                    playback_source,
                    playback_target_rate,
                )
            elif self._rate_changed and not coordinator_attempted:
                current_force_rate = _get_current_pipewire_force_rate()
                if current_force_rate == self.measurement_rate:
                    try:
                        # Set the rate of the playback context directly. Do not
                        # briefly restore the idle/default rate first: that would
                        # create a second transition before the sink is aligned.
                        _set_pipewire_force_rate(target_rate)
                    except Exception as exc:
                        force_rate_owned = False
                        logger.warning("Measurement sample-rate session force-rate restore failed: %s", exc)
                else:
                    force_rate_owned = False
                    logger.warning(
                        "Measurement sample-rate session restore skipped after external force-rate change: "
                        "current_force_rate=%s measurement_rate=%s restore_value=%s target_rate=%s",
                        current_force_rate,
                        self.measurement_rate,
                        restore_value,
                        target_rate,
                    )

            if self._rate_changed and not coordinator_attempted and _playback_state_before_measurement is not None:
                saved_rate = _playback_state_before_measurement.get("expected_rate")
                if isinstance(saved_rate, int) and saved_rate > 0 and saved_rate != self.measurement_rate:
                    playback_stream_stale_after_measurement = True
                    if _playback_state_before_measurement.get("source") == "radio":
                        radio_stream_stale_after_measurement = True

            try:
                runtime_restore_rate = playback_target_rate or restore_value
                if runtime_restore_rate <= 0:
                    status = get_samplerate_status()
                    runtime_restore_rate = int(
                        status.get("configured_default_rate")
                        or status.get("clock_rate")
                        or DEFAULT_SAMPLE_RATE
                    )

                rate_ready = True
                if coordinator_attempted:
                    rate_ready = False
                elif playback_target_rate and force_rate_owned:
                    if playback_source == "radio":
                        rate_ready = await _ensure_playback_samplerate_force(
                            playback_target_rate,
                            "radio-restart-after-measurement",
                            allow_measurement_session=True,
                            policy=samplerate_orchestration.RADIO_POLICY,
                        )
                    else:
                        rate_ready = await _wait_for_samplerate_alignment(
                            playback_target_rate, timeout_ms=3500,
                        )

                measurement_only_restore = not playback_target_rate or playback_source not in {"local", "radio", "spotify"}
                if rate_ready and not coordinator_attempted and measurement_only_restore:
                    await _sync_subwoofer_runtime_at_rate(runtime_restore_rate, _rate_lock_held=True)
                else:
                    logger.warning(
                        "Measurement sample-rate session runtime restore deferred until playback sink aligns: "
                        "source=%s target_rate=%s",
                        playback_source, runtime_restore_rate,
                    )
            except Exception as exc:
                logger.warning("Measurement sample-rate session runtime restore failed: %s", exc)
        finally:
            completed_generation = self.generation
            self.active = False
            self.active_manual_job_ids.clear()
            self.active_auto_sub_job_id = None
            self.active_spl_job_ids.clear()
            self.close_requested = False
            self.deferred_release_pending = False
            self._playback_captured = False
            self._rate_changed = False
            self.original_force_rate = 0
            _playback_state_before_measurement = None
            _radio_state_before_measurement = None
            self.generation += 1
            logger.info(
                "Measurement sample-rate session released: generation=%s next_generation=%s restore_rate=%s",
                completed_generation,
                self.generation,
                restore_value,
            )

    async def run_watchdog(self) -> None:
        while True:
            try:
                # Heartbeat expiry is not an audio-session close signal. Browser
                # timer suspension can pause heartbeats while the window remains
                # open; only the explicit close request may restore the rate.
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Measurement sample-rate session watchdog failed: %s", exc)
                await asyncio.sleep(5.0)


# Global instances (initialized on startup)
settings = None
player_instance = None
library_scanner = None
downloader = None
easyeffects_manager = None
measurement_store = None
measurement_sr_session = None
peak_monitor = None
subwoofer_runtime = None
subwoofer_runtime_link_watch_task = None
hardware_controller = None
peak_monitor_playback_armed = False
peak_monitor_transition_lock = None
peak_monitor_context_signature = None
easyeffects_preset_load_lock = None
source_transition_lock = None
playback_transition_coordinator: PlaybackTransitionCoordinator | None = None
coordinator_recovery_lock: asyncio.Lock | None = None
coordinator_recovery_inflight_signature: str | None = None
coordinator_recovery_last_signature: tuple[str, str | None] | None = None
coordinator_last_successful_commit_id: str | None = None
external_input_loopback_module_id = None
external_input_loopback_source_name = None
bluetooth_input_source_name = None
bluetooth_monitor_task = None
bluetooth_agent_process = None
spotify_playerctl_watch_task = None
spotify_playerctl_detect_task = None
spotify_state_refresh_task = None
spotify_state_poll_task = None
spotify_playerctl_last_trigger_at = 0.0
playback_samplerate_force_rate = None
current_source_mode = SOURCE_MODE_APP_PLAYBACK
latest_spotify_state = None
current_footer_owner = "local"
last_measurement_window_seen_at = 0.0
silent_active_recovery_attempts: set[str] = set()
silent_active_watch_tasks: dict[str, asyncio.Task] = {}
latest_player_state_seq_seen = 0
playback_transition_generation = 0
playback_intent_generation = 0
current_track_info = None
last_track_info = None
last_radio_track_info = None
radio_reconnect_task = None
radio_reconnect_attempts = 0
radio_reconnect_url = None
radio_reconnect_active_since = 0.0
radio_stream_stale_after_measurement = False
radio_metadata_service = RadioMetadataService()
_radio_state_before_measurement: dict[str, Any] | None = None
playback_stream_stale_after_measurement = False
_playback_state_before_measurement: dict[str, Any] | None = None
samplerate_drift_signature: tuple[Any, ...] | None = None
samplerate_drift_readbacks = 0
playback_queue = []
playback_queue_original = []
playback_queue_index = -1
playback_queue_mode = "app_replace"
queue_advancing = False
queue_transition_target_url = None
playback_queue_loop = False
playback_queue_shuffle = False
single_track_loop = False
spl_calibration_noise_process = None
spl_calibration_noise_file: Path | None = None
spl_calibration_restore_state: dict[str, Any] | None = None
SPL_CALIBRATION_JOB_ID = "spl-calibration"


def _hardware_sink_for_transition() -> str:
    """Resolve the physical sink used by the coordinator output gate."""
    status = get_samplerate_status()
    relevant_sink = status.get("relevant_sink") or {}
    output_key = str(relevant_sink.get("name") or "").strip()
    if output_key:
        return output_key
    overview = get_audio_output_overview()
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


def _player_is_running(player=None) -> bool:
    """Return player availability without requiring a concrete MPV class.

    The production wrapper exposes ``_running``.  Keeping the adapter tolerant
    of small player doubles is useful for the coordinator's failure-path tests
    and does not weaken the production check: an explicit ``False`` still
    means unavailable.
    """
    player = player if player is not None else player_instance
    return bool(player is not None and getattr(player, "_running", True))


def _load_player_paused(path: str) -> None:
    """Load a target through the explicit paused-load contract."""
    if not _player_is_running():
        raise RuntimeError("MPV player is not available")
    player_instance.set_pause(True)
    try:
        player_instance.loadfile(path, mode="replace", start_paused=True)
    except TypeError as exc:
        # Compatibility for a minimal adapter that predates the explicit
        # keyword.  The real MPVWrapper implements start_paused; the fallback
        # still keeps the source paused before and after load.
        if "start_paused" not in str(exc) and "keyword" not in str(exc):
            raise
        player_instance.loadfile(path, mode="replace")
    player_instance.set_pause(True)


class FxrouteTransitionRuntime(TransitionRuntime):
    """Concrete runtime adapter; graph mutations only enter via the coordinator."""

    def __init__(self) -> None:
        self._output_key: str | None = None
        self._staged_target_url: str | None = None
        self._previous_force_rate: int | None = None

    async def read_hardware_mute(self) -> bool:
        self._output_key = _hardware_sink_for_transition()
        return await asyncio.to_thread(_read_hardware_sink_mute, self._output_key)

    async def set_hardware_mute(self, muted: bool, transition_id: str) -> None:
        output_key = self._output_key or _hardware_sink_for_transition()
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
        rate = dict(get_samplerate_status())
        diagnosis = await _playback_graph_diagnosis(
            target_rate=target_rate,
            require_source=False,
        )
        result = dict(diagnosis)
        result["active_rate"] = rate.get("active_rate")
        result["force_rate"] = rate.get("force_rate")
        result["measurement_rate_aligned"] = bool(
            rate.get("active_rate") == target_rate
            and rate.get("force_rate") in {None, 0, target_rate}
        )
        result["repairable_link_loss"] = _measurement_session_link_loss_is_repairable(
            result,
            target_rate=target_rate,
        )
        return result

    async def reconcile_measurement_session_graph(self, _target_rate: int) -> None:
        """Repair only existing 2.1/2.2 production links."""
        await _coordinator_reconcile_subwoofer_links_only()

    async def read_transition_snapshot(self, request: TransitionRequest) -> dict[str, Any]:
        self._staged_target_url = None
        self._previous_force_rate = _get_current_pipewire_force_rate()
        state = dict(player_instance.state if player_instance else {})
        try:
            rate = dict(get_samplerate_status())
        except Exception:
            rate = {}
        snapshot = {
            "player": state,
            "active_rate": rate.get("active_rate"),
            "force_rate": rate.get("force_rate"),
            "source": request.source,
            "target_url": request.target_url,
            "current_track": dict(current_track_info or {}),
            "playback_intent_generation": playback_intent_generation,
        }
        if request.operation == "output-mode-switch":
            snapshot["output_mode_overview"] = copy.deepcopy(get_audio_output_overview())
            snapshot["output_mode_config"] = copy.deepcopy(
                samplerate._load_raw_audio_output_mode()
            )
            snapshot["ee_active_preset"] = (
                easyeffects_manager.get_active_preset()
                if easyeffects_manager is not None
                else None
            )
            snapshot["spotify"] = await get_spotify_ui_state()
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
    ) -> None:
        """Finish a failed MPV handoff without mixing old and new context.

        The Coordinator has already attenuated and paused the source before it
        calls this hook. If MPV still exposes the exact pre-transition file,
        the committed context can remain available for an intentional retry.
        Once a new target was staged (or the old file disappeared), clear the
        active metadata and queue, while preserving ``last_track_info``.
        """
        global current_track_info, current_footer_owner
        global playback_queue, playback_queue_original, playback_queue_index
        global playback_queue_mode, queue_transition_target_url
        global playback_queue_loop, playback_queue_shuffle, single_track_loop

        if request.source not in {"local", "radio"}:
            return

        previous_state = dict((snapshot or {}).get("player") or {})
        current_state = dict(player_instance.state if player_instance else {})
        previous_file = previous_state.get("current_file")
        current_file = current_state.get("current_file")
        previous_context_unchanged = (
            not target_staged
            and current_file == previous_file
            and not (current_file is None and current_track_info)
        )

        if previous_context_unchanged:
            live_track = current_track_info or {}
            if current_file and live_track.get("url") not in {None, current_file}:
                snapshot_track = dict((snapshot or {}).get("current_track") or {})
                if snapshot_track.get("url") == current_file:
                    current_track_info = snapshot_track
                else:
                    previous_context_unchanged = False
            if previous_context_unchanged:
                # ``/api/play`` prepares queue metadata before entering the
                # Coordinator. If it fails before MPV stages the new target,
                # that queue is still uncommitted even though the old MPV
                # file remains valid. Clear it rather than exposing a new
                # queue next to the retained old track. Recovery/graph-only
                # attempts do not own queue metadata and keep it intact.
                if request.operation in {"play", "queue"} or request.native_queue:
                    try:
                        _clear_playback_queue()
                    except Exception:
                        logger.warning(
                            "Failed to clear uncommitted queue during transition abort",
                            exc_info=True,
                        )
                        playback_queue = []
                        playback_queue_original = []
                        playback_queue_index = -1
                        playback_queue_mode = "app_replace"
                        queue_transition_target_url = None
                        playback_queue_loop = False
                        playback_queue_shuffle = False
                        single_track_loop = False
                return

        # The target was staged, the old file disappeared, or the active
        # metadata no longer matches MPV. Stop the physical target first and
        # then invalidate only the active context. last_track_info is
        # deliberately untouched so the caller can offer a retry.
        if _player_is_running():
            try:
                set_volume = getattr(player_instance, "set_volume", None)
                if callable(set_volume):
                    set_volume(0)
            except Exception:
                logger.warning(
                    "Failed to attenuate MPV during failed transition abort",
                    exc_info=True,
                )
            try:
                stop_playback = getattr(player_instance, "stop_playback", None)
                if callable(stop_playback):
                    stop_playback()
                else:
                    player_instance.set_pause(True)
            except Exception:
                logger.warning(
                    "Failed to stop staged MPV target during transition abort",
                    exc_info=True,
                )

        try:
            _clear_playback_queue()
        except Exception:
            # A broken native playlist command must not leave the application
            # queue pointing at a target which is no longer active.
            logger.warning(
                "Failed to trim native queue during transition abort",
                exc_info=True,
            )
            playback_queue = []
            playback_queue_original = []
            playback_queue_index = -1
            playback_queue_mode = "app_replace"
            queue_transition_target_url = None
            playback_queue_loop = False
            playback_queue_shuffle = False
            single_track_loop = False

        current_track_info = None
        current_footer_owner = "local"
        _mark_player_state_authoritative(player_instance.state if player_instance else {})

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
        if expected_source == "spotify":
            try:
                spotify_state = await get_spotify_ui_state()
            except Exception:
                return False
            expected_identities = _spotify_snapshot_identity_values(intent)
            if not expected_identities:
                expected_identities = _spotify_snapshot_identity_values({
                    "target_url": request.target_url,
                    "track_info": request.target_track,
                })
            live_identities = _spotify_identity_values(spotify_state)
            status = str(spotify_state.get("status") or "").strip().lower()
            if status not in {"playing", "paused"}:
                return False
            if not expected_identities or not live_identities.intersection(expected_identities):
                return False
            expected_generation = intent.get("intent_generation")
            return not (
                isinstance(expected_generation, int)
                and expected_generation != playback_intent_generation
            )

        expected_id = intent.get("id")
        expected_url = intent.get("url") or intent.get("path") or request.target_url
        live_track = current_track_info or {}
        if str(live_track.get("source") or "") != expected_source:
            return False
        if expected_id not in {None, ""} and live_track.get("id") != expected_id:
            return False
        if expected_url and live_track.get("url") != expected_url:
            return False

        state = dict(player_instance.state if player_instance else {})
        current_file = state.get("current_file")
        if not current_file or state.get("ended"):
            return False
        expected_file = intent.get("current_file")
        if expected_file and current_file != expected_file:
            return False
        expected_generation = intent.get("intent_generation")
        if (
            isinstance(expected_generation, int)
            and expected_generation != playback_intent_generation
        ):
            return False
        return True

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
                await spotify_pause()
                released = await _wait_for_pipewire_spotify_release()
                if not released:
                    await asyncio.sleep(SOURCE_HANDOFF_SETTLE_MS / 1000)
                return
            if request.operation in {"measurement-entry", "output-mode-switch"}:
                spotify_state = await get_spotify_ui_state()
                if _is_spotify_playback_active(spotify_state):
                    await spotify_pause()
                    if not await _wait_for_pipewire_spotify_release():
                        raise RuntimeError(
                            "active Spotify sink input did not quiesce before guarded graph transition"
                        )
                return
            local_state = dict(player_instance.state if player_instance else {})
            local_track = current_track_info or {}
            if (
                local_track.get("source") in {"local", "radio"}
                and _has_local_footer_context(local_state)
            ):
                await pause_local_playback_for_spotify_broadcast()
            return
        if request.source not in {"local", "radio"}:
            return
        spotify_state = await get_spotify_ui_state()
        if _is_spotify_playback_active(spotify_state):
            await pause_spotify_for_local_playback_broadcast()
            # The output gate is already closed at this Coordinator stage.
            # Do not touch rate/DSP/helper state until the active Spotify
            # stream has disappeared. Corked historical inputs are ignored by
            # the read-only release helper and therefore need not vanish.
            if not await _wait_for_pipewire_spotify_release():
                raise RuntimeError(
                    "active Spotify sink input did not quiesce before MPV handoff"
                )
        if not _player_is_running():
            return
        state = player_instance.state
        set_volume = getattr(player_instance, "set_volume", None)
        if callable(set_volume):
            set_volume(0)
        if (
            request.operation == "recovery"
            and state.get("current_file")
            and not request.rate_change
        ):
            player_instance.set_pause(True)
            return
        # A healthy same-rate replacement keeps the existing MPV/PipeWire
        # stream alive and only quiets it.  A real rate change must release the
        # old stream before the target-rate negotiation begins.
        player_instance.set_pause(True)
        should_release = bool(
            request.rate_change
            and request.operation not in {"measurement-entry", "output-mode-switch"}
            and state.get("current_file")
        )
        if should_release:
            player_instance.stop_playback()
            released = await _wait_for_pipewire_mpv_release()
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
            if not _player_is_running():
                raise RuntimeError("MPV player is not available")
            set_volume = getattr(player_instance, "set_volume", None)
            if callable(set_volume):
                set_volume(0)
            _load_player_paused(request.target_url)
            if not await _wait_for_player_current_file(request.target_url):
                raise RuntimeError("local target did not settle while paused")
            live_rate = await _wait_for_player_audio_samplerate(
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
            return live_rate

        if request.source != "radio" or not request.reload_source:
            return request.target_rate
        if not request.target_url:
            return request.target_rate
        if not _player_is_running():
            raise RuntimeError("MPV player is not available")

        previous_rate = _get_player_audio_samplerate()
        set_volume = getattr(player_instance, "set_volume", None)
        if callable(set_volume):
            set_volume(0)
        _load_player_paused(request.target_url)
        if not await _wait_for_player_current_file(request.target_url):
            raise RuntimeError("radio target stream did not settle while paused")
        live_rate = await _wait_for_radio_live_rate_after_load(
            previous_rate,
            playback_transition_generation,
        )
        if not isinstance(live_rate, int) or live_rate <= 0:
            live_rate = RADIO_EXPECTED_SAMPLE_RATE_HZ
            logger.warning(
                "Radio target rate unavailable while paused; using safe fallback=%s url=%s",
                live_rate,
                request.target_url,
            )
        self._staged_target_url = request.target_url
        return live_rate

    async def establish_target_rate(self, request: TransitionRequest) -> None:
        if request.graph_only:
            return
        if not isinstance(request.target_rate, int) or request.target_rate <= 0:
            raise RuntimeError("Playback transition has no target sample rate")
        try:
            status = dict(get_samplerate_status())
        except Exception:
            status = {}
        if (
            status.get("active_rate") == request.target_rate
            and status.get("force_rate") in {None, 0, request.target_rate}
        ):
            logger.info(
                "Playback transition target-rate no-op: rate=%s operation=%s source=%s",
                request.target_rate,
                request.operation,
                request.source,
            )
            return
        aligned = await _ensure_playback_samplerate_force(
            request.target_rate,
            f"coordinator:{request.operation}:{request.source}",
            allow_measurement_session=(request.operation == "measurement-restore"),
            policy=samplerate_orchestration.RADIO_POLICY,
        )
        if not aligned:
            status = get_samplerate_status()
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
        return await _coordinator_establish_effects_and_helper(
            request,
            previous_force_rate=self._previous_force_rate,
        )

    async def prepare_target_source(self, request: TransitionRequest) -> None:
        if request.graph_only:
            return
        if request.source == "spotify":
            return
        if not _player_is_running():
            raise RuntimeError("MPV player is not available")

        if not request.reload_source and not request.should_play:
            player_instance.set_pause(True)
            return

        set_volume = getattr(player_instance, "set_volume", None)
        if callable(set_volume):
            set_volume(0)

        if request.native_queue:
            set_shuffle = getattr(player_instance, "set_shuffle", None)
            if callable(set_shuffle):
                # Disable any legacy MPV-side permutation before staging or
                # jumping within the explicit FXRoute queue order.
                set_shuffle(False)
            queue_tracks = tuple(request.native_queue)
            jump_index = request.native_queue_jump
            if jump_index is not None:
                if jump_index < 0 or jump_index >= len(queue_tracks):
                    raise RuntimeError(f"native MPV queue index is out of range: {jump_index}")
                player_instance.set_pause(True)
                player_instance.set_playlist_pos(jump_index)
                self._staged_target_url = request.target_url
            elif request.reload_source:
                start_index = request.native_queue_index
                if start_index is None:
                    start_index = 0
                if start_index < 0 or start_index >= len(queue_tracks):
                    raise RuntimeError(f"native MPV queue start index is out of range: {start_index}")
                first_url = str(queue_tracks[0].get("url") or "")
                if not first_url:
                    raise RuntimeError("native MPV queue has no first URL")
                _load_player_paused(first_url)
                for queued_track in queue_tracks[1:]:
                    queued_url = str(queued_track.get("url") or "")
                    if not queued_url:
                        raise RuntimeError("native MPV queue contains an empty URL")
                    player_instance.loadfile(queued_url, mode="append")
                player_instance.set_loop_playlist(bool(request.native_queue_loop))
                player_instance.set_playlist_pos(start_index)
                self._staged_target_url = request.target_url
            else:
                player_instance.set_pause(True)

            if not await _wait_for_player_current_file(request.target_url):
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
                player_instance.set_loop_playlist(False)
                set_shuffle = getattr(player_instance, "set_shuffle", None)
                if callable(set_shuffle):
                    set_shuffle(False)
                _load_player_paused(request.target_url)
                self._staged_target_url = request.target_url
                if not await _wait_for_player_current_file(request.target_url):
                    raise RuntimeError("target MPV stream did not settle while paused")
            else:
                player_instance.set_pause(True)

        if (
            request.operation == "measurement-restore"
            and request.source == "local"
            and request.restore_position is not None
        ):
            position = max(0.0, float(request.restore_position))
            seek = getattr(player_instance, "seek", None)
            if not callable(seek):
                raise RuntimeError("MPV position restore is not available")
            player_instance.set_pause(True)
            seek(position)
            get_property = getattr(player_instance, "get_property", None)
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
                "Measurement local playback position restored under output gate: "
                "url=%s position=%.3f",
                request.target_url,
                position,
            )

        if not await _ensure_mpv_to_easyeffects_links():
            raise RuntimeError("target source to EasyEffects links were not confirmed")
        if not request.should_play:
            player_instance.set_pause(True)

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
                await spotify_pause()
                data = {"status": "Paused"}
            if request.should_play and data.get("status") not in {"Playing", "playing"}:
                raise RuntimeError(f"Spotify did not enter Playing state: {data}")
            return
        if not _player_is_running():
            raise RuntimeError("MPV player is not available")
        player_instance.set_pause(not request.should_play)
        if not request.should_play:
            return
        deadline = time.monotonic() + 1.8
        last_readback: dict[str, Any] = {}
        get_property = getattr(player_instance, "get_property", None)
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
                state = player_instance.state
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
        manager = easyeffects_manager
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
        """Re-apply the canonical DSP work point after a rate/EE mutation."""
        if not (request.rate_change or dsp_reinitialized):
            return {"stabilized": True, "no_op": True}
        if (
            not request.should_play
            and request.operation not in {"measurement-entry", "output-mode-switch"}
        ):
            return {"stabilized": True, "no_op": True}

        manager = easyeffects_manager
        if manager is None:
            return {"stabilized": True, "no_op": True}

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
        # settles, then ramps back to the canonical work point.
        await asyncio.to_thread(
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
        rate = dict(get_samplerate_status())
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
        graph = await _playback_graph_diagnosis(
            graph_overview,
            source=request.source,
            target_rate=request.target_rate,
            require_source=(
                request.should_play
                or (request.operation == "output-mode-switch" and bool(request.target_url))
            ),
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
        set_volume = getattr(player_instance, "set_volume", None) if _player_is_running() else None
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
            rate = dict(get_samplerate_status())
        except Exception:
            rate = {}
        state = dict(player_instance.state if player_instance else {})
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
            spotify_state = await get_spotify_ui_state()
            expected_status = "Playing" if request.should_play else "Paused"
            if request.should_play and spotify_state.get("status") != expected_status:
                raise RuntimeError(f"Spotify status was not confirmed: {spotify_state.get('status')}")
            if not request.should_play and spotify_state.get("status") == "Playing":
                raise RuntimeError("Spotify pause state was not confirmed at transition commit")

        spotify_stream_rate = None
        if request.source == "spotify" and request.should_play:
            spotify_stream_rate = await _wait_for_spotify_sink_input_samplerate(
                expected_rate=request.target_rate,
            )
            if (
                isinstance(request.target_rate, int)
                and spotify_stream_rate != request.target_rate
            ):
                raise RuntimeError(
                    "Spotify stream rate mismatch at commit: "
                    f"expected={request.target_rate} actual={spotify_stream_rate}"
                )

        if isinstance(request.target_rate, int) and request.target_rate > 0:
            if rate.get("active_rate") != request.target_rate:
                raise RuntimeError(
                    f"hardware rate mismatch at commit: expected={request.target_rate} actual={rate.get('active_rate')}"
                )
            if rate.get("force_rate") not in {None, 0, request.target_rate}:
                raise RuntimeError(f"force-rate mismatch at commit: {rate.get('force_rate')}")
        if (
            spotify_stream_rate is not None
            and rate.get("active_rate") != spotify_stream_rate
        ):
            raise RuntimeError(
                "Spotify stream and hardware rates disagree at commit: "
                f"spotify={spotify_stream_rate} hardware={rate.get('active_rate')}"
            )

        graph_complete = await _playback_graph_links_complete(
            source=request.source,
            target_rate=request.target_rate,
            require_source=True,
        )
        if not graph_complete:
            raise RuntimeError("production playback links were not complete at commit")

        helper_rate = None
        try:
            output_mode = (get_audio_output_overview().get("output_mode") or {}).get("mode")
            if output_mode in OUTPUT_MODE_SUBWOOFER_MODES:
                if subwoofer_runtime is None:
                    raise RuntimeError("subwoofer helper runtime is not available at commit")
                helper_snapshot = subwoofer_runtime.snapshot()
                helper_rate = _helper_argument_sample_rate(helper_snapshot)
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
        if easyeffects_manager and require_effects_runtime:
            preset = await asyncio.to_thread(easyeffects_manager.get_active_preset)
            if not preset:
                raise RuntimeError("EasyEffects active preset was not confirmed at commit")
            extras = easyeffects_manager.load_global_extras()
            effects_runtime = await self._read_and_validate_effects_runtime(extras)
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
        status = dict(get_samplerate_status())
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
                await _playback_graph_diagnosis(
                    target_rate=request.target_rate,
                    require_source=False,
                )
            )
        signatures = [str(item.get("signature")) for item in readbacks]
        if not all(item.get("links_complete") for item in readbacks) or len(set(signatures)) != 1:
            final = readbacks[-1] if readbacks else {}
            _log_playback_graph_diagnosis(
                final,
                target_rate=int(request.target_rate or 0),
                reason="measurement-entry",
                detail=request.detail,
            )
            raise RuntimeError(
                "measurement entry canonical graph did not reach two stable readbacks"
            )

        if request.source in {"local", "radio"} and request.target_url:
            state = dict(player_instance.state if player_instance else {})
            if state.get("current_file") != request.target_url:
                raise RuntimeError(
                    "measurement entry changed the loaded music source: "
                    f"expected={request.target_url} actual={state.get('current_file')}"
                )
            if not state.get("paused"):
                raise RuntimeError("music source was not left paused for measurement")
        elif request.source == "spotify":
            spotify_state = await get_spotify_ui_state()
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

        rate = dict(get_samplerate_status())
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
                await _playback_graph_diagnosis(
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
            _log_playback_graph_diagnosis(
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
            spotify_stream_rate = await _wait_for_spotify_sink_input_samplerate(
                expected_rate=target_rate,
            )
            if spotify_stream_rate != target_rate:
                raise RuntimeError(
                    "Spotify stream rate mismatch during output-mode commit: "
                    f"expected={target_rate} actual={spotify_stream_rate}"
                )

        if request.source in {"local", "radio"} and request.target_url:
            state = dict(player_instance.state if player_instance else {})
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
            spotify_state = await get_spotify_ui_state()
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
        result = persist_audio_output_mode(request.output_mode_config)
        return {
            "output_mode_persisted": True,
            "output_mode": dict(result.get("output_mode") or {}),
        }

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
        persist_audio_output_mode(old_config)
        if easyeffects_manager is not None and old_preset:
            current_preset = easyeffects_manager.get_active_preset()
            if current_preset != old_preset:
                easyeffects_manager.load_preset(old_preset, convolver_sample_rate_hz=request.target_rate)
        await _sync_subwoofer_runtime(
            old_overview,
            reason="coordinator-output-mode-rollback",
            target_overview=old_overview,
        )
        if old_mode in OUTPUT_MODE_SUBWOOFER_MODES:
            await _coordinator_reconcile_subwoofer_links_only()
        rollback_request = replace(request, output_mode_target=old_overview)
        await self._verify_output_mode_rollback(rollback_request, old_mode)

    async def _verify_output_mode_rollback(
        self,
        request: TransitionRequest,
        _old_mode: Any,
    ) -> None:
        readbacks = [
            await _playback_graph_diagnosis(
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
        if request.source in {"local", "radio"} and _player_is_running():
            previous_volume = previous_player.get("volume")
            if isinstance(previous_volume, (int, float)):
                await self.set_source_volume(int(round(previous_volume)), transition_id)
            should_play = bool(
                previous_player.get("playing")
                and not previous_player.get("paused")
                and not previous_player.get("ended")
            )
            player_instance.set_pause(not should_play)
            state = dict(player_instance.state if player_instance else {})
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
                await spotify_pause()

    async def reconcile_post_start_graph(self, request: TransitionRequest) -> dict[str, Any]:
        """Run the bounded final graph reconciliation before staged commit."""
        return await _coordinator_reconcile_post_start_graph(request)

    async def verify_committed_transition(self, request: TransitionRequest) -> dict[str, Any]:
        return await self._verify_transition(request, require_source_volume=True)

    async def pause_source_after_failure(self, request: TransitionRequest) -> None:
        if request.source == "spotify":
            try:
                await spotify_pause()
            except Exception:
                pass
            return
        if _player_is_running():
            try:
                # Keep the safety invariant even when a lightweight test
                # adapter (or a partially initialized player) does not expose
                # a volume setter: pausing the source must never be skipped
                # because the preceding best-effort attenuation failed.
                set_volume = getattr(player_instance, "set_volume", None)
                if callable(set_volume):
                    try:
                        set_volume(0)
                    except Exception:
                        logger.warning("Failed to attenuate MPV after transition failure", exc_info=True)
                player_instance.set_pause(True)
            except Exception:
                logger.warning("Failed to pause MPV after transition failure", exc_info=True)


def _coordinator_target_rate(source: str, track: Mapping[str, Any] | None = None) -> int | None:
    track = track or {}
    if source == "spotify":
        return SPOTIFY_PREARM_SAMPLE_RATE_HZ
    if source == "radio":
        return int(track.get("sample_rate_hz") or RADIO_EXPECTED_SAMPLE_RATE_HZ)
    value = track.get("sample_rate_hz")
    return int(value) if isinstance(value, int) and value > 0 else None


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


async def _coordinator_current_playback_context() -> dict[str, Any]:
    """Read the currently owned source without mutating either transport."""
    local_state = dict(player_instance.state if player_instance else {})
    local_track = dict(current_track_info or {})
    spotify_state = await get_spotify_ui_state()
    local_active = _is_local_playback_active(local_state)
    spotify_active = _is_spotify_playback_active(spotify_state)

    if local_active and local_track.get("source") in {"local", "radio"}:
        return {
            "source": local_track.get("source"),
            "target_url": local_state.get("current_file"),
            "target_track": local_track,
            "should_play": True,
            "spotify": spotify_state,
        }
    if spotify_active:
        track_id = spotify_state.get("trackId") or spotify_state.get("url")
        return {
            "source": "spotify",
            "target_url": str(track_id or "") or None,
            "target_track": {
                "source": "spotify",
                "id": spotify_state.get("trackId"),
                "url": track_id,
                "title": spotify_state.get("title"),
                "artist": spotify_state.get("artist"),
                "sample_rate_hz": SPOTIFY_PREARM_SAMPLE_RATE_HZ,
            },
            "should_play": True,
            "spotify": spotify_state,
        }
    if local_track.get("source") in {"local", "radio"} and local_state.get("current_file"):
        return {
            "source": local_track.get("source"),
            "target_url": local_state.get("current_file"),
            "target_track": local_track,
            "should_play": bool(local_state.get("playing") and not local_state.get("paused")),
            "spotify": spotify_state,
        }
    if current_footer_owner == "spotify" and spotify_state.get("trackId"):
        return {
            "source": "spotify",
            "target_url": str(spotify_state.get("trackId")),
            "target_track": {
                "source": "spotify",
                "id": spotify_state.get("trackId"),
                "url": spotify_state.get("trackId"),
                "title": spotify_state.get("title"),
                "artist": spotify_state.get("artist"),
                "sample_rate_hz": SPOTIFY_PREARM_SAMPLE_RATE_HZ,
            },
            "should_play": False,
            "spotify": spotify_state,
        }
    return {
        "source": "local",
        "target_url": None,
        "target_track": {},
        "should_play": False,
        "spotify": spotify_state,
    }


def _coordinator_rate_change(target_rate: int | None) -> bool:
    if not isinstance(target_rate, int) or target_rate <= 0:
        return False
    try:
        status = get_samplerate_status()
    except Exception:
        return True
    return not (
        status.get("active_rate") == target_rate
        and status.get("force_rate") in {None, 0, target_rate}
    )


def _playback_transition_is_active() -> bool:
    return bool(
        playback_transition_coordinator is not None
        and playback_transition_coordinator.transition_active
    )


def _coordinator_commit_context_id() -> str | None:
    """Return the newest successful Coordinator commit context."""
    global coordinator_last_successful_commit_id
    result = getattr(playback_transition_coordinator, "last_result", None)
    if getattr(result, "committed", False):
        transition_id = getattr(result, "transition_id", None)
        if transition_id:
            coordinator_last_successful_commit_id = str(transition_id)
    return coordinator_last_successful_commit_id


async def _recovery_context_is_valid(request: TransitionRequest) -> bool:
    """Validate a watcher recovery against the still-committed live source."""
    expected_context = request.recovery_commit_context_id
    expected_source = request.recovery_source or request.source
    expected_url = request.recovery_url or request.target_url
    if not expected_context or expected_source != request.source or not expected_url:
        return False
    if _coordinator_commit_context_id() != expected_context:
        return False
    if _playback_transition_is_active():
        return False
    gate = getattr(playback_transition_coordinator, "gate", None)
    if gate is not None and bool(getattr(gate, "failure_latched", False)):
        return False

    if expected_source == "spotify":
        try:
            spotify_state = await get_spotify_ui_state()
        except Exception:
            return False
        if spotify_state.get("status") != "Playing":
            return False
        live_identity = str(
            spotify_state.get("trackId")
            or spotify_state.get("url")
            or ""
        )
        return live_identity == str(expected_url)

    state = dict(player_instance.state if player_instance else {})
    if state.get("current_file") != expected_url or state.get("ended"):
        return False
    # A paused/loaded committed local context is still a valid context for a
    # graph/rate observation; a missing active file is not.
    live_track = current_track_info or {}
    if live_track:
        if live_track.get("source") != expected_source:
            return False
        if live_track.get("url") != expected_url:
            return False
    return True


async def _run_coordinated_transition(request: TransitionRequest):
    """Run one transition and maintain the legacy generation readback guard."""
    global playback_transition_generation, playback_transition_coordinator
    global coordinator_last_successful_commit_id
    if playback_transition_coordinator is None:
        # Unit callers may invoke an endpoint without running FastAPI's
        # lifespan.  Production still initializes the same singleton during
        # startup; lazy construction keeps the ownership boundary identical.
        playback_transition_coordinator = PlaybackTransitionCoordinator(
            FxrouteTransitionRuntime(),
            gate_state_path=_playback_gate_state_path(),
        )
    playback_transition_generation += 1
    try:
        result = await playback_transition_coordinator.execute(request)
        if getattr(result, "committed", False):
            transition_id = getattr(result, "transition_id", None)
            if transition_id:
                coordinator_last_successful_commit_id = str(transition_id)
        return result
    finally:
        # The Coordinator owns transition cleanup.  This outer guard only
        # closes the legacy generation interval, including cancellation.
        if playback_transition_generation % 2:
            playback_transition_generation += 1


async def _request_coordinated_recovery(
    track: Mapping[str, Any],
    reason: str,
    *,
    reload_source: bool = False,
    graph_only: bool = False,
    diagnosis: Mapping[str, Any] | None = None,
) -> None:
    """Request one deduplicated recovery through the Coordinator.

    Watchers pass the canonical graph signature.  Identical observations are
    coalesced, so a persistent bypass link cannot create a two-second full
    handoff loop.
    """
    global coordinator_recovery_lock, coordinator_recovery_inflight_signature, coordinator_recovery_last_signature
    if playback_transition_coordinator is None or not track:
        return
    source = str(track.get("source") or "")
    if source not in {"local", "radio", "spotify"}:
        return
    target_rate = _coordinator_target_rate(source, track)
    if not isinstance(target_rate, int) or target_rate <= 0:
        return

    if source == "spotify":
        try:
            should_play = (await get_spotify_ui_state()).get("status") == "Playing"
        except Exception:
            should_play = False
    else:
        state = dict(player_instance.state if player_instance else {})
        should_play = bool(
            state.get("current_file")
            and state.get("playing")
            and not state.get("paused")
            and not state.get("ended")
        )

    operation = "graph-reconcile" if graph_only else "recovery"
    rate_change = False if graph_only else _coordinator_rate_change(target_rate)
    effective_reload = False if graph_only else reload_source
    signature = json.dumps(
        {
            "source": source,
            "url": str(track.get("url") or track.get("id") or ""),
            "target_rate": target_rate,
            "should_play": should_play,
            "rate_change": rate_change,
            "reload_source": effective_reload,
            "graph_only": graph_only,
            "graph": (diagnosis or {}).get("signature") if diagnosis else None,
        },
        sort_keys=True,
    )
    # Capture the context at observation time, before waiting for another
    # recovery attempt to release the lock.  A duplicate queued while that
    # attempt is running must still deduplicate against the attempt's
    # original context, even if the first attempt commits successfully.
    attempt_commit_context_id = _coordinator_commit_context_id()
    if not attempt_commit_context_id:
        logger.info(
            "Coordinator recovery discarded without a committed context: reason=%s source=%s url=%s",
            reason,
            source,
            track.get("url") or track.get("id"),
        )
        return
    if coordinator_recovery_lock is None:
        coordinator_recovery_lock = asyncio.Lock()

    async with coordinator_recovery_lock:
        # Capture the successful commit context before this attempt starts.
        # A successful recovery advances the Coordinator's last_result while
        # it is running; that new ID must not be stored as the context of the
        # attempt that just produced it.
        dedupe_key = (signature, attempt_commit_context_id)
        if (
            signature == coordinator_recovery_inflight_signature
            or dedupe_key == coordinator_recovery_last_signature
        ):
            logger.info(
                "Coordinator recovery deduplicated: reason=%s graph_only=%s signature=%s commit_context=%s",
                reason,
                graph_only,
                signature,
                attempt_commit_context_id,
            )
            return
        # Do not latch a request that merely arrived while another transition
        # was active.  The next watcher observation must still be allowed to
        # retry after that transition has committed.
        if playback_transition_coordinator.transition_active:
            logger.info("Coordinator recovery deferred while another transition is active: reason=%s", reason)
            return
        attempted = False
        try:
            observed_url = str(track.get("url") or track.get("id") or "") or None
            recovery_track = dict(track)
            if source == "spotify" and observed_url:
                recovery_track.setdefault("url", observed_url)
            request = TransitionRequest(
                operation=operation,
                source=source,
                target_rate=target_rate,
                target_url=observed_url,
                target_track=recovery_track,
                should_play=should_play,
                rate_change=rate_change,
                reload_source=effective_reload,
                graph_only=graph_only,
                detail=reason,
                recovery_commit_context_id=attempt_commit_context_id,
                recovery_source=source,
                recovery_url=observed_url,
            )
            # This is deliberately immediately before execution.  A watcher
            # must never resurrect a target after an intervening user action,
            # failed handoff, or committed source change.
            if not await _recovery_context_is_valid(request):
                logger.info(
                    "Coordinator recovery discarded after context recheck: reason=%s source=%s url=%s commit_context=%s",
                    reason,
                    source,
                    observed_url,
                    attempt_commit_context_id,
                )
                return
            coordinator_recovery_inflight_signature = signature
            attempted = True
            result = await _run_coordinated_transition(request)
            if getattr(result, "committed", False) and source in {"local", "radio"} and isinstance(result.target_rate, int) and result.target_rate > 0:
                track["sample_rate_hz"] = result.target_rate
                if (
                    current_track_info
                    and current_track_info.get("source") == source
                    and current_track_info.get("url") == track.get("url")
                ):
                    current_track_info["sample_rate_hz"] = result.target_rate
        except PlaybackTransitionFailure as exc:
            logger.warning("Coordinator recovery failed: %s", exc.as_status())
        finally:
            coordinator_recovery_inflight_signature = None
            # Keep the observation deduplicated only within the Coordinator
            # commit context in which this attempt began.  A successful
            # recovery advances the context, so a later identical fault is
            # eligible for a fresh attempt.
            if attempted:
                coordinator_recovery_last_signature = (
                    signature,
                    attempt_commit_context_id,
                )


def _transition_error_http(exc: PlaybackTransitionFailure) -> HTTPException:
    return HTTPException(status_code=500, detail=exc.as_status())


def _mark_playback_intent_changed() -> None:
    """Advance the measurement-restore intent token after a user action."""
    global playback_intent_generation
    playback_intent_generation += 1


def _commit_coordinated_track(track_info: Mapping[str, Any], *, source: str) -> None:
    global current_track_info, last_track_info, last_radio_track_info, current_footer_owner
    track = dict(track_info)
    _mark_playback_intent_changed()
    current_track_info = track
    last_track_info = track
    current_footer_owner = "spotify" if source == "spotify" else "local"
    if source == "radio":
        last_radio_track_info = dict(track)
    if source == "local":
        _record_local_track_started(track)
    _mark_player_state_authoritative(player_instance.state if player_instance else {})

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket connected: {len(self.active_connections)} active")

    def disconnect(self, websocket: WebSocket) -> bool:
        if websocket not in self.active_connections:
            return False
        self.active_connections.remove(websocket)
        logger.info(f"WebSocket disconnected: {len(self.active_connections)} active")
        return True

    async def broadcast(self, message: dict):
        data = json.dumps(message)
        dead = []
        for connection in list(self.active_connections):
            try:
                if connection.client_state.name != "CONNECTED":
                    dead.append(connection)
                    continue
                await connection.send_text(data)
            except Exception as e:
                logger.debug(f"WebSocket send failed: {e}")
                dead.append(connection)
        for conn in set(dead):
            self.disconnect(conn)

manager = ConnectionManager()


def _choose_unique_path(path: Path) -> Path:
    """Thin wrapper: unique path selection lives in zip_album (REFACTOR-008)."""
    return zip_album.choose_unique_path(path)


def _choose_unique_dir(path: Path) -> Path:
    """Thin wrapper: unique dir selection lives in zip_album (REFACTOR-008)."""
    return zip_album.choose_unique_dir(path)


def _is_safe_relative_zip_path(name: str) -> Optional[Path]:
    """Thin wrapper: ZIP traversal protection lives in zip_album (REFACTOR-008)."""
    return zip_album.is_safe_relative_zip_path(name)


def _extract_zip_album(zip_path: Path, target_root: Path) -> dict:
    """Thin wrapper: ZIP album extraction lives in zip_album (REFACTOR-008)."""
    return zip_album.extract_zip_album(zip_path, target_root)


def _parse_m3u_entries(content: str) -> List[str]:
    """Thin wrapper: M3U parsing lives in playlist_io (REFACTOR-002)."""
    return playlist_io.parse_m3u_entries(content)


def _playlist_download_filename(name: str) -> str:
    """Thin wrapper: download filename logic lives in playlist_io (REFACTOR-002)."""
    return playlist_io.playlist_download_filename(name)


def _track_relative_m3u_path(track) -> str:
    """Thin wrapper: M3U path rendering lives in playlist_io (REFACTOR-002)."""
    return playlist_io.track_relative_m3u_path(track)


def _build_m3u_for_playlist(playlist) -> str:
    """Thin wrapper: M3U export content lives in playlist_io (REFACTOR-002)."""
    return playlist_io.build_m3u_for_playlist(playlist)


def _resolve_m3u_track_ids(entries: List[str], base_dir: Optional[Path] = None, tracks=None) -> List[str]:
    """Thin wrapper: M3U entry resolution lives in playlist_io (REFACTOR-002)."""
    return playlist_io.resolve_m3u_track_ids(entries, base_dir=base_dir, tracks=tracks)


def _import_m3u_playlist(name: str, content: str, base_dir: Optional[Path] = None, tracks=None) -> Optional[dict]:
    """Thin wrapper: M3U playlist import lives in playlist_io (REFACTOR-002)."""
    return playlist_io.import_m3u_playlist(name, content, base_dir=base_dir, tracks=tracks)


def _native_mpv_playlist_is_effectively_current_only() -> bool:
    """Confirm that MPV already has no queued entry beyond the current file."""
    if not _player_is_running():
        return False
    state = dict(getattr(player_instance, "state", {}) or {})
    if not state.get("current_file"):
        return False
    get_property = getattr(player_instance, "get_property", None)
    if not callable(get_property):
        return False
    try:
        playlist_count = get_property("playlist-count")
    except Exception:
        return False
    return isinstance(playlist_count, int) and playlist_count <= 1


def _native_mpv_playlist_error_is_stale(exc: Exception) -> bool:
    """Recognize only errors caused by an already-gone playlist entry."""
    message = str(exc).lower()
    command_error = "playlist-remove" in message or "playlist-clear" in message
    stale_state = any(
        marker in message
        for marker in (
            "already gone",
            "already removed",
            "playlist entry",
            "playlist index",
            "no such entry",
            "out of range",
            "is empty",
        )
    )
    return command_error and stale_state


def _reduce_native_mpv_playlist_to_current() -> None:
    """Keep MPV's current file and atomically drop all queued entries.

    ``playlist-clear`` is explicitly idempotent with respect to the currently
    played entry.  The former index loop was vulnerable to a concurrent MPV
    playlist change: a stale ``playlist-remove`` then aborted ``/api/play``
    before the Coordinator could receive the new request.
    """
    if not _player_is_running():
        return
    clear_playlist = getattr(player_instance, "clear_playlist", None)
    if not callable(clear_playlist):
        # Keep small adapters used by maintenance/test contexts compatible;
        # production MPVWrapper exposes clear_playlist explicitly.
        send_command = getattr(player_instance, "_send_command", None)
        if callable(send_command):
            clear_playlist = lambda: send_command("playlist-clear")
    if not callable(clear_playlist):
        raise RuntimeError("MPV adapter cannot clear its native playlist")
    try:
        clear_playlist()
    except Exception as exc:
        # A shortened playlist can race the clear command.  Only suppress this
        # narrow stale-entry case after a read-only proof that MPV is already
        # reduced to its current file; genuine IPC failures remain fatal.
        if _native_mpv_playlist_error_is_stale(exc) and _native_mpv_playlist_is_effectively_current_only():
            logger.info("Native MPV playlist was already reduced while clearing stale entries: %s", exc)
            return
        raise


def _clear_playback_queue():
    global playback_queue, playback_queue_original, playback_queue_index, playback_queue_mode, queue_transition_target_url, playback_queue_loop, playback_queue_shuffle, single_track_loop
    was_native = playback_queue_mode == "native_mpv"
    if was_native:
        _reduce_native_mpv_playlist_to_current()
        _reset_mpv_loop_state()
    playback_queue = []
    playback_queue_original = []
    playback_queue_index = -1
    playback_queue_mode = "app_replace"
    queue_transition_target_url = None
    playback_queue_loop = False
    playback_queue_shuffle = False
    single_track_loop = False


def _queue_payload() -> dict:
    return {
        "active": len(playback_queue) > 1,
        "index": playback_queue_index,
        "count": len(playback_queue),
        "mode": playback_queue_mode,
        "tracks": [dict(item) for item in playback_queue],
        "loop": playback_queue_loop or single_track_loop,
        "shuffle": playback_queue_shuffle,
    }


def _sync_track_context_from_queue_index(index: int) -> Optional[dict]:
    global current_track_info, last_track_info, playback_queue_index
    if index < 0 or index >= len(playback_queue):
        return None
    playback_queue_index = index
    track = dict(playback_queue[index])
    current_track_info = track
    last_track_info = track
    return track


def _reset_mpv_loop_state() -> None:
    if not player_instance or not player_instance._running:
        return
    set_loop_playlist = getattr(player_instance, "set_loop_playlist", None)
    if callable(set_loop_playlist):
        set_loop_playlist(False)
    set_loop_file = getattr(player_instance, "set_loop_file", None)
    if callable(set_loop_file):
        set_loop_file(False)
    set_shuffle = getattr(player_instance, "set_shuffle", None)
    if callable(set_shuffle):
        set_shuffle(False)


def _current_track_matches(expected_track: dict | None) -> bool:
    if not expected_track:
        return False
    live_track = current_track_info or {}
    if not (
        live_track.get("source") == expected_track.get("source")
        and live_track.get("url") == expected_track.get("url")
        and live_track.get("id") == expected_track.get("id")
    ):
        return False
    expected_url = expected_track.get("url")
    current_file = (player_instance.state if player_instance else {}).get("current_file")
    if expected_url and current_file and current_file != expected_url:
        return False
    return True


def _playback_state_matches_track(state: dict | None, track: dict | None) -> bool:
    return playback_state.playback_state_matches_track(state, track)


async def _wait_for_player_current_file(expected_url: str | None, timeout_ms: int = 1600) -> bool:
    if not expected_url or not player_instance:
        return False
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while time.monotonic() <= deadline:
        state = player_instance.state
        if state.get("current_file") == expected_url:
            return True
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
    return False


def _brief_sink_inputs(entries: list[dict]) -> list[dict]:
    return sink_inputs.brief_sink_inputs(entries)


def _active_unmuted_sink_inputs(entries: list[dict]) -> list[dict]:
    return sink_inputs.active_unmuted_sink_inputs(entries)


def _silent_active_source_links_present(source: str, links_text: str, output_mode: dict) -> bool:
    if source == "spotify":
        source_link_ok = "spotify:output_FL" in links_text and "easyeffects_sink:playback_FL" in links_text
    else:
        source_link_ok = "mpv:output_FL" in links_text and "easyeffects_sink:playback_FL" in links_text
    if not source_link_ok:
        return False

    mode = output_mode.get("mode") or OUTPUT_MODE_STEREO
    if mode != OUTPUT_MODE_STEREO:
        return True
    output_key = str(output_mode.get("effective_output_key") or "").strip()
    if not output_key:
        return True
    return output_key in links_text and "ee_soe_output_level:output_FL" in links_text


def _silent_active_snapshot(
    *,
    source: str,
    owner: str,
    track: dict | None,
    playback_state: dict,
    spotify_state: dict,
    source_inputs: list[dict],
    all_inputs: list[dict],
    links_text: str,
    overview: dict,
    peak_snapshot: dict,
) -> dict:
    output_mode = overview.get("output_mode") or {}
    return {
        "source": source,
        "owner": owner,
        "track": {
            "id": (track or {}).get("id"),
            "title": (track or {}).get("title"),
            "url": (track or {}).get("url"),
        },
        "playback": {
            "playing": playback_state.get("playing"),
            "paused": playback_state.get("paused"),
            "current_file": playback_state.get("current_file"),
            "source_volume": playback_state.get("volume"),
            "output_volume": get_output_volume_safe(100),
        },
        "spotify": {
            "status": spotify_state.get("status"),
            "title": spotify_state.get("title"),
            "source_volume": spotify_state.get("source_volume"),
            "output_volume": spotify_state.get("volume"),
        } if spotify_state else {},
        "output_mode": {
            "mode": output_mode.get("mode"),
            "effective_output_key": output_mode.get("effective_output_key"),
            "effective_output_rate": output_mode.get("effective_output_rate"),
            "runtime": (output_mode.get("runtime") or {}) if isinstance(output_mode.get("runtime"), dict) else {},
        },
        "source_inputs": _brief_sink_inputs(source_inputs),
        "all_sink_inputs": _brief_sink_inputs(all_inputs),
        "source_link_present": _silent_active_source_links_present(source, links_text, output_mode),
        "links_excerpt": "\n".join(
            line for line in links_text.splitlines()
            if any(
                token in line
                for token in (
                    "mpv",
                    "spotify",
                    "easyeffects_sink",
                    "ee_soe_output_level",
                    str(output_mode.get("effective_output_key") or "").strip(),
                )
                if token
            )
        )[:4000],
        "levels": {
            "output_peak": peak_snapshot,
            "pre_level": None,
            "post_level": peak_snapshot.get("vu_db"),
        },
    }


def _schedule_silent_active_watch(
    *,
    source: str,
    signature: str,
    track: dict | None = None,
    spotify_state: dict | None = None,
) -> None:
    if not signature:
        return
    existing = silent_active_watch_tasks.get(signature)
    if existing and not existing.done():
        return
    task = asyncio.create_task(
        _silent_active_watch_after_settle(source=source, signature=signature, track=track, spotify_state=spotify_state),
        name=f"silent-active-watch:{source}",
    )
    silent_active_watch_tasks[signature] = task


async def _silent_active_watch_after_settle(
    *,
    source: str,
    signature: str,
    track: dict | None = None,
    spotify_state: dict | None = None,
) -> None:
    try:
        await asyncio.sleep(SILENT_ACTIVE_SETTLE_SECONDS)
        await _check_and_recover_silent_active(source=source, signature=signature, track=track, spotify_state=spotify_state)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Silent-active watch failed: source=%s signature=%s error=%s", source, signature, exc)
    finally:
        task = silent_active_watch_tasks.get(signature)
        if task is asyncio.current_task():
            silent_active_watch_tasks.pop(signature, None)


async def _check_and_recover_silent_active(
    *,
    source: str,
    signature: str,
    track: dict | None = None,
    spotify_state: dict | None = None,
) -> None:
    if signature in silent_active_recovery_attempts:
        return
    if not peak_monitor:
        return

    playback_state = player_instance.state if player_instance and player_instance._running else {}
    live_track = current_track_info or {}
    owner = current_footer_owner or source
    if source in {"local", "radio"}:
        if not track or not _current_track_matches(track):
            return
        if not _is_local_playback_active(playback_state):
            return
        source_inputs = _list_mpv_sink_inputs()
        source_volume = playback_state.get("volume")
    elif source == "spotify":
        spotify_state = await get_spotify_ui_state()
        if not _is_spotify_playback_active(spotify_state):
            return
        source_inputs = _list_spotify_sink_inputs()
        source_volume = spotify_state.get("source_volume")
        live_track = {
            "id": spotify_state.get("trackId"),
            "title": spotify_state.get("title"),
            "artist": spotify_state.get("artist"),
            "source": "spotify",
        }
    else:
        return

    if not _active_unmuted_sink_inputs(source_inputs):
        return
    try:
        if int(round(float(source_volume if source_volume is not None else 100))) <= 0:
            return
    except (TypeError, ValueError):
        pass
    if get_output_volume_safe(100) <= 0:
        return

    overview = get_audio_output_overview()
    output_mode = overview.get("output_mode") or {}
    links_result = _run_debug_command(["pw-link", "-l"], 2.0)
    links_text = links_result.get("stdout") or ""
    if not _silent_active_source_links_present(source, links_text, output_mode):
        return

    peak_snapshot = peak_monitor.snapshot()
    vu_db = peak_snapshot.get("vu_db")
    if not isinstance(vu_db, (int, float)) or vu_db > SILENT_ACTIVE_FLOOR_DB:
        return

    # Skip when no current sample is available: vu_db then only reflects the
    # technical -60 dB floor, not real silence. Freshness (vu_fresh) is the
    # sample-validity signal of peak_monitor.snapshot(); the peak-hold
    # "detected" flag is unrelated to sample validity and must not gate the
    # diagnosis.
    if not peak_snapshot.get("vu_fresh"):
        logger.info(
            "SILENT-ACTIVE-DIAG skip: peak_samples_stale vu_db=%s source=%s signature=%s",
            vu_db, source, signature,
        )
        return

    # Skip during measurement window or while EE preset is actively loading.
    # The audio path is in transition; not a real silent-active condition.
    if _is_measurement_window_open() or (
        easyeffects_preset_load_lock is not None and easyeffects_preset_load_lock.locked()
    ):
        logger.info(
            "SILENT-ACTIVE-DIAG skip: transition_window measurement_open=%s ee_preset_loading=%s source=%s signature=%s",
            _is_measurement_window_open(),
            easyeffects_preset_load_lock.locked() if easyeffects_preset_load_lock is not None else False,
            source, signature,
        )
        return

    all_inputs = _list_sink_inputs()
    snapshot = _silent_active_snapshot(
        source=source,
        owner=owner,
        track=live_track,
        playback_state=playback_state,
        spotify_state=spotify_state or {},
        source_inputs=source_inputs,
        all_inputs=all_inputs,
        links_text=links_text,
        overview=overview,
        peak_snapshot=peak_snapshot,
    )
    logger.warning(
        "Silent-active playback detected (log-only, recovery disabled): %s",
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
    )
    # PATCH silent-active-neutralize (2026-07-07):
    # Automatic loadfile() / spotify-handoff recovery is disabled. It was
    # breaking normal library starts by reloading mid-playback, which then
    # triggered "Stopping peak monitor" via the buffering pause state.
    # Existing peak-monitor / link-watch / owner logic remains the source
    # of truth for state corrections. silent_active_recovery_attempts is
    # still recorded so duplicate triggers for the same source/url are
    # naturally suppressed by the existing dedupe path.
    silent_active_recovery_attempts.add(signature)
    logger.warning(
        "SILENT-ACTIVE-DIAG recovery_suppressed: would_have_recovered source=%s signature=%s vu_db=%s action=log_only",
        source, signature, vu_db,
    )
    return


def _playback_transition_context_is_current(generation: int | None) -> bool:
    """Return true only for the committed playback context captured by a task."""
    return (
        isinstance(generation, int)
        and generation == playback_transition_generation
        and generation % 2 == 0
    )




async def _easyeffects_output_ports_present() -> bool:
    """Readback: are ee_soe_output_level:output_FL/FR exposed right now?"""
    try:
        links_text = await _run_pw_link_command("-io")
    except Exception:
        return False
    return (
        "ee_soe_output_level:output_FL" in links_text
        and "ee_soe_output_level:output_FR" in links_text
    )


async def _wait_for_easyeffects_output_ports(timeout_ms: int) -> bool:
    """Poll pw-link -io until the EasyEffects output ports are exposed.

    Readback-driven replacement for fixed sleeps: the handoff only proceeds
    to the helper sync once the EE output ports actually exist. The EE
    preset sync (step 3) triggers the port recreation; this wait observes it.
    """
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while True:
        if await _easyeffects_output_ports_present():
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)


async def _playback_graph_diagnosis(
    audio_overview: dict | None = None,
    *,
    source: str | None = None,
    target_rate: int | None = None,
    require_source: bool = False,
) -> dict:
    """Return the one canonical, read-only production-graph snapshot.

    The Coordinator and every watcher use this function unchanged.  In
    stereo the valid output is EE -> hardware.  In 2.1/2.2 the helper owns
    every hardware output and direct EE -> hardware links are explicitly
    invalid, even when the helper links are also present.
    """
    result = {
        "mode": None,
        "output_key": "",
        "ee_ports": False,
        "helper_ports": None,
        "helper_active": None,
        "helper_rate": None,
        "helper_rate_matches": None,
        "links": {},
        "source_links": {},
        "source_links_complete": None,
        "direct_ee_to_hw_present": False,
        "links_complete": False,
        "bypass_only": False,
        "port_identities": {
            "source": (),
            "source_target": (),
            "ee": (),
            "helper": (),
            "output": (),
        },
        "signature": "unreadable",
    }
    try:
        overview = audio_overview or get_audio_output_overview()
        output_mode = overview.get("output_mode") or {}
        mode = output_mode.get("mode")
        output_key = str(output_mode.get("effective_output_key") or "").strip()
        result["mode"] = mode
        result["output_key"] = output_key
        if not output_key:
            return result
        io_text = await _run_pw_link_command("-io")
        link_text = await _run_pw_link_command("-l")
    except Exception:
        return result

    ee_fl = "ee_soe_output_level:output_FL"
    ee_fr = "ee_soe_output_level:output_FR"
    helper = "fxroute_21_stage1"
    helper_port_names = tuple(
        f"{helper}:{port}"
        for port in ("input_L", "input_R", "output_1", "output_2", "output_3", "output_4")
    )
    output_port_names = tuple(
        f"{output_key}:playback_{channel}"
        for channel in (
            "FL",
            "FR",
            *(("RL", "RR") if mode in OUTPUT_MODE_SUBWOOFER_MODES and mode != OUTPUT_MODE_SUBWOOFER_21 else ()),
        )
    )
    result["ee_ports"] = ee_fl in io_text and ee_fr in io_text

    source_node = "spotify" if source == "spotify" else "mpv" if source in {"local", "radio"} else None
    source_port_names = (
        f"{source_node}:output_FL",
        f"{source_node}:output_FR",
    ) if source_node else ()
    source_target_port_names = (
        "easyeffects_sink:playback_FL",
        "easyeffects_sink:playback_FR",
    )
    result["port_identities"] = {
        "source": tuple(port for port in source_port_names if port in io_text),
        "source_target": tuple(
            port for port in source_target_port_names if port in io_text
        ),
        "ee": tuple(port for port in (ee_fl, ee_fr) if port in io_text),
        "helper": tuple(port for port in helper_port_names if port in io_text),
        "output": tuple(port for port in output_port_names if port in io_text),
    }
    if source_node:
        result["source_links"] = {
            f"{source_node}:output_FL -> easyeffects_sink:playback_FL": _contains_link(
                link_text, f"{source_node}:output_FL", "easyeffects_sink:playback_FL"
            ),
            f"{source_node}:output_FR -> easyeffects_sink:playback_FR": _contains_link(
                link_text, f"{source_node}:output_FR", "easyeffects_sink:playback_FR"
            ),
        }
        result["source_links_complete"] = all(result["source_links"].values())
    elif require_source:
        result["source_links_complete"] = False

    source_ok = result["source_links_complete"] is not False
    if mode not in OUTPUT_MODE_SUBWOOFER_MODES:
        result["links"] = {
            f"{ee_fl} -> {output_key}:playback_FL": _contains_link(
                link_text, ee_fl, f"{output_key}:playback_FL"
            ),
            f"{ee_fr} -> {output_key}:playback_FR": _contains_link(
                link_text, ee_fr, f"{output_key}:playback_FR"
            ),
        }
        result["links_complete"] = (
            source_ok and result["ee_ports"] and all(result["links"].values())
        )
    else:
        result["helper_ports"] = all(
            port in io_text for port in helper_port_names
        )
        result["links"] = {
            f"{ee_fl} -> {helper}:input_L": _contains_link(link_text, ee_fl, f"{helper}:input_L"),
            f"{ee_fr} -> {helper}:input_R": _contains_link(link_text, ee_fr, f"{helper}:input_R"),
            f"{helper}:output_1 -> {output_key}:playback_FL": _contains_link(
                link_text, f"{helper}:output_1", f"{output_key}:playback_FL"
            ),
            f"{helper}:output_2 -> {output_key}:playback_FR": _contains_link(
                link_text, f"{helper}:output_2", f"{output_key}:playback_FR"
            ),
        }
        if mode != OUTPUT_MODE_SUBWOOFER_21:
            result["links"][f"{helper}:output_3 -> {output_key}:playback_RL"] = _contains_link(
                link_text, f"{helper}:output_3", f"{output_key}:playback_RL"
            )
            result["links"][f"{helper}:output_4 -> {output_key}:playback_RR"] = _contains_link(
                link_text, f"{helper}:output_4", f"{output_key}:playback_RR"
            )

        direct_links = {
            f"{ee_fl} -> {output_key}:playback_FL": _contains_link(
                link_text, ee_fl, f"{output_key}:playback_FL"
            ),
            f"{ee_fr} -> {output_key}:playback_FR": _contains_link(
                link_text, ee_fr, f"{output_key}:playback_FR"
            ),
        }
        result["direct_ee_to_hw_present"] = any(direct_links.values())
        helper_topology_complete = (
            result["ee_ports"]
            and result["helper_ports"]
            and all(result["links"].values())
        )
        try:
            helper_snapshot = subwoofer_runtime.snapshot() if subwoofer_runtime is not None else {}
            result["helper_active"] = bool(helper_snapshot.get("active"))
            result["helper_rate"] = _helper_argument_sample_rate(helper_snapshot)
        except Exception:
            result["helper_active"] = False
        result["helper_rate_matches"] = (
            bool(result["helper_active"])
            and (
                target_rate is None
                or result["helper_rate"] == target_rate
            )
        )
        helper_valid = bool(result["helper_rate_matches"])
        result["bypass_only"] = bool(
            source_ok and helper_topology_complete and helper_valid and result["direct_ee_to_hw_present"]
        )
        # Direct EE -> hardware links are part of the invalid state, not an
        # optional extra.  This is the key invariant shared by commit and
        # watcher readback.
        result["links_complete"] = bool(
            source_ok
            and helper_topology_complete
            and helper_valid
            and not result["direct_ee_to_hw_present"]
        )

    result["signature"] = "|".join(
        (
            str(result.get("mode")),
            str(result.get("output_key")),
            str(result.get("ee_ports")),
            str(result.get("helper_ports")),
            str(result.get("helper_active")),
            str(result.get("helper_rate")),
            str(result.get("helper_rate_matches")),
            str(result.get("source_links_complete")),
            str(result.get("direct_ee_to_hw_present")),
            str(result.get("links_complete")),
            ";".join(
                f"{key}={value}"
                for key, value in sorted(result.get("source_links", {}).items())
            ),
            ";".join(f"{key}={value}" for key, value in sorted(result.get("links", {}).items())),
            ";".join(
                f"{key}={','.join(str(port) for port in ports)}"
                for key, ports in sorted(result.get("port_identities", {}).items())
            ),
        )
    )
    return result


def _missing_playback_graph_links(
    diagnosis: Mapping[str, Any],
    *,
    include_source: bool = False,
) -> list[str]:
    """Names of missing canonical links from a graph diagnosis."""
    missing = [
        link for link, present in (diagnosis.get("links") or {}).items()
        if not present
    ]
    if include_source:
        missing = [
            *[
                link
                for link, present in (diagnosis.get("source_links") or {}).items()
                if not present
            ],
            *missing,
        ]
    return missing


def _measurement_session_link_loss_is_repairable(
    diagnosis: Mapping[str, Any],
    *,
    target_rate: int,
) -> bool:
    """Allow only the known EE->helper input-link drift during measurement."""
    if diagnosis.get("links_complete"):
        return False
    if diagnosis.get("mode") not in OUTPUT_MODE_SUBWOOFER_MODES:
        return False
    if diagnosis.get("ee_ports") is not True:
        return False
    if diagnosis.get("helper_ports") is not True:
        return False
    if diagnosis.get("helper_active") is not True:
        return False
    if diagnosis.get("helper_rate_matches") is not True:
        return False
    if diagnosis.get("measurement_rate_aligned") is not True:
        return False
    if diagnosis.get("direct_ee_to_hw_present"):
        return False
    if diagnosis.get("helper_rate") != target_rate:
        return False
    missing = set(_missing_playback_graph_links(diagnosis))
    repairable = {
        "ee_soe_output_level:output_FL -> fxroute_21_stage1:input_L",
        "ee_soe_output_level:output_FR -> fxroute_21_stage1:input_R",
    }
    return bool(missing) and missing.issubset(repairable)


def _log_playback_graph_diagnosis(
    diagnosis: dict,
    *,
    target_rate: int,
    reason: str,
    detail: str,
) -> None:
    """Log every missing graph component individually (EE ports, helper
    ports, each missing link) so a failed handoff is diagnosable."""
    logger.warning(
        "Playback handoff graph incomplete: mode=%s output_key=%s target_rate=%s "
        "ee_ports=%s helper_ports=%s helper_active=%s helper_rate=%s "
        "direct_bypass=%s source_links=%s missing_links=%s reason=%s detail=%s",
        diagnosis.get("mode"),
        diagnosis.get("output_key"),
        target_rate,
        diagnosis.get("ee_ports"),
        diagnosis.get("helper_ports"),
        diagnosis.get("helper_active"),
        diagnosis.get("helper_rate"),
        diagnosis.get("direct_ee_to_hw_present"),
        diagnosis.get("source_links_complete"),
        _missing_playback_graph_links(diagnosis),
        reason,
        detail,
    )


async def _repair_stereo_output_links_once(diagnosis: dict) -> None:
    """Repair only missing stereo EE->hardware links, without reloading EE."""
    output_key = str(diagnosis.get("output_key") or "").strip()
    if not output_key:
        raise RuntimeError("Playback handoff repair failed: missing stereo output target")
    expected = (
        ("ee_soe_output_level:output_FL", f"{output_key}:playback_FL"),
        ("ee_soe_output_level:output_FR", f"{output_key}:playback_FR"),
    )
    links_text = await _run_pw_link_command("-l")
    for source, target in expected:
        if _contains_link(links_text, source, target):
            continue
        logger.info("Radio handoff repairing EE->hardware link: %s -> %s", source, target)
        await _connect_ports((source,), target)


async def _coordinator_reconcile_subwoofer_links_only() -> None:
    """Repair only the 2.1/2.2 link topology, never restart the helper."""
    if subwoofer_runtime is None:
        raise RuntimeError("subwoofer helper runtime is not available")
    reconcile = getattr(subwoofer_runtime, "reclean_direct_easyeffects_links", None)
    if not callable(reconcile):
        raise RuntimeError("subwoofer runtime has no link-only reconciliation")
    await reconcile()


def _post_start_graph_links_are_repairable(
    diagnosis: Mapping[str, Any],
    *,
    include_source: bool = False,
) -> bool:
    """Return true only when a diagnosis contains stable ports and link-only drift.

    Source-link loss is repairable only for the output-mode commit, where the
    source was deliberately re-created under the gate.  Helper lifecycle or
    rate problems, direct bypass links, and missing port identities remain
    fatal.
    """
    if not diagnosis.get("output_key") or not diagnosis.get("ee_ports"):
        return False
    if diagnosis.get("source_links_complete") is not True and not include_source:
        return False
    if diagnosis.get("direct_ee_to_hw_present"):
        return False
    if diagnosis.get("mode") in OUTPUT_MODE_SUBWOOFER_MODES:
        if diagnosis.get("helper_ports") is not True:
            return False
        if diagnosis.get("helper_active") is not True:
            return False
        if diagnosis.get("helper_rate_matches") is not True:
            return False

    identities = {
        str(port)
        for ports in (diagnosis.get("port_identities") or {}).values()
        if isinstance(ports, (tuple, list, set, frozenset))
        for port in ports
    }
    for link in _missing_playback_graph_links(
        diagnosis,
        include_source=include_source,
    ):
        try:
            source, target = link.split(" -> ", 1)
        except ValueError:
            return False
        if source not in identities or target not in identities:
            return False
    return True


async def _relink_post_start_missing_production_links(
    diagnosis: Mapping[str, Any],
    *,
    include_source: bool = False,
) -> bool:
    """Relink only the missing production edges from the current readback.

    The endpoint names come from the immediately preceding canonical
    readback, so a recreated PipeWire port cannot be mistaken for an old
    identity.  ``_connect_ports`` is idempotent for an already existing edge.
    """
    missing = _missing_playback_graph_links(
        diagnosis,
        include_source=include_source,
    )
    if not missing:
        return False
    if not _post_start_graph_links_are_repairable(
        diagnosis,
        include_source=include_source,
    ):
        raise RuntimeError(
            "post-start graph was not link-only drift with stable current ports"
        )
    for link in missing:
        source, target = link.split(" -> ", 1)
        logger.info(
            "Coordinator post-start graph relinking current production edge: %s -> %s",
            source,
            target,
        )
        await _connect_ports((source,), target)
    return True


async def _coordinator_reconcile_post_start_graph(
    request: TransitionRequest,
) -> dict[str, Any]:
    """Reconcile a transient production-link loss before staged commit.

    This is a final Coordinator-owned step shared by Local, Radio and
    Spotify.  It performs at most one targeted link-only repair and then
    requires two identical complete graph readbacks.  No preset, helper or
    watcher recovery is entered here.
    """
    target_rate = request.target_rate
    target_overview = (
        copy.deepcopy(request.output_mode_target)
        if request.operation == "output-mode-switch" and request.output_mode_target
        else None
    )
    graph_source = (
        request.source
        if request.target_url or request.should_play
        else None
    )
    if not isinstance(target_rate, int) or target_rate <= 0:
        return {
            "graph_complete": True,
            "post_start_graph_reconciled": False,
            "post_start_graph_links_relinked": False,
        }

    initial = await _playback_graph_diagnosis(
        target_overview,
        source=graph_source,
        target_rate=target_rate,
        require_source=graph_source is not None,
    )
    include_source = request.operation == "output-mode-switch"
    initial_missing = _missing_playback_graph_links(
        initial,
        include_source=include_source,
    )
    if not initial.get("links_complete") and not initial_missing:
        _log_playback_graph_diagnosis(
            initial,
            target_rate=target_rate,
            reason=f"post-start-{request.operation}",
            detail=request.detail,
        )
        raise RuntimeError(
            "post-start graph readback was incomplete without link-only drift"
        )
    relinked = await _relink_post_start_missing_production_links(
        initial,
        include_source=include_source,
    )

    readbacks: list[dict[str, Any]] = []
    for _ in range(POST_START_GRAPH_STABILITY_READBACKS):
        readbacks.append(
            await _playback_graph_diagnosis(
                target_overview,
                source=graph_source,
                target_rate=target_rate,
                require_source=graph_source is not None,
            )
        )

    signatures = [str(readback.get("signature")) for readback in readbacks]
    stable_complete = bool(
        len(readbacks) == POST_START_GRAPH_STABILITY_READBACKS
        and all(readback.get("links_complete") for readback in readbacks)
        and len(set(signatures)) == 1
    )
    if not stable_complete:
        final = readbacks[-1] if readbacks else initial
        _log_playback_graph_diagnosis(
            final,
            target_rate=target_rate,
            reason=f"post-start-{request.operation}",
            detail=request.detail,
        )
        raise RuntimeError(
            "post-start production graph did not reach two stable canonical readbacks"
        )

    return {
        "graph_complete": True,
        "post_start_graph_reconciled": True,
        "post_start_graph_links_relinked": relinked,
        "graph_signature": signatures[-1],
    }


async def _coordinator_establish_effects_and_helper(
    request: TransitionRequest,
    *,
    previous_force_rate: int | None = None,
    ee_port_timeout_ms: int = PLAYBACK_HANDOFF_EE_PORT_TIMEOUT_MS,
) -> dict[str, Any]:
    """Build the effects/helper graph inside the Coordinator-owned gate.

    This is intentionally smaller than the removed legacy handoff.  Rate
    alignment belongs to ``establish_target_rate``; source loading belongs to
    the following adapter stages; this function only performs the idempotent
    EE/helper/link work and then uses the canonical graph readback.
    """
    del previous_force_rate  # kept in the adapter call for source compatibility
    target_rate = request.target_rate
    if not isinstance(target_rate, int) or target_rate <= 0:
        return {
            "dsp_reinitialized": False,
            "preset_reloaded": False,
            "helper_rebuilt": False,
            "links_reconciled": False,
        }

    overview = copy.deepcopy(request.output_mode_target) if request.output_mode_target else get_audio_output_overview()
    mode = (overview.get("output_mode") or {}).get("mode")
    preset_reloaded = False
    helper_rebuilt = False
    links_reconciled = False
    diagnosis = await _playback_graph_diagnosis(
        overview,
        target_rate=target_rate,
        require_source=False,
    )

    if request.graph_only:
        if not diagnosis.get("bypass_only"):
            raise RuntimeError(
                "graph-only reconciliation requested for a non-bypass graph: "
                f"signature={diagnosis.get('signature')}"
            )
        await _coordinator_reconcile_subwoofer_links_only()
        links_reconciled = True
    else:
        needs_preset = not diagnosis.get("ee_ports")
        # A healthy same-rate graph must not reload its preset.  A convolver
        # that needs the target rate is relevant only during a real rate
        # transition; missing EE ports remain a genuine recovery condition.
        if request.rate_change and not needs_preset and easyeffects_manager is not None:
            requires_convolver_reload = getattr(
                easyeffects_manager,
                "active_preset_requires_samplerate_reload",
                None,
            )
            if callable(requires_convolver_reload):
                try:
                    needs_preset = bool(
                        await asyncio.to_thread(
                            requires_convolver_reload,
                            target_rate,
                        )
                    )
                except Exception as exc:
                    # Preserve a functioning active graph when its preset file
                    # cannot be inspected.  A missing/broken graph still takes
                    # the reload path above; an unknown convolver is not a
                    # reason to reload every rate transition.
                    logger.warning(
                        "Coordinator could not inspect active preset for convolver "
                        "sample-rate reload: %s",
                        exc,
                    )
        if request.operation == "output-mode-switch" and easyeffects_manager is not None:
            compare = easyeffects_manager.load_compare_state()
            active_side = compare.get("activeSide") if compare.get("activeSide") in {"A", "B"} else None
            target_preset = (
                compare.get("presetA") if active_side == "A" else
                compare.get("presetB") if active_side == "B" else
                None
            )
            current_preset = easyeffects_manager.get_active_preset()
            if target_preset and current_preset != target_preset:
                easyeffects_manager.load_preset(target_preset, convolver_sample_rate_hz=target_rate)
                needs_preset = True
                preset_reloaded = True
                logger.info(
                    "Coordinator output-mode switch loaded compare-active preset under gate: %s",
                    target_preset,
                )
        if needs_preset and not preset_reloaded:
            await _sync_easyeffects_preset_for_playback_samplerate(
                sample_rate_hz=target_rate,
                reason=f"coordinator-{request.operation}",
                detail=request.detail,
            )
            preset_reloaded = True
        if not await _wait_for_easyeffects_output_ports(ee_port_timeout_ms):
            raise RuntimeError(
                "Coordinator effects stage failed: EasyEffects output ports were not confirmed"
            )

        if request.operation == "output-mode-switch":
            await _sync_subwoofer_runtime(
                audio_overview=overview,
                reason="coordinator-output-mode-switch",
                _rate_lock_held=False,
                target_overview=overview,
            )
            helper_rebuilt = mode in OUTPUT_MODE_SUBWOOFER_MODES
            if mode in OUTPUT_MODE_SUBWOOFER_MODES:
                await _coordinator_reconcile_subwoofer_links_only()
                links_reconciled = True
        elif mode in OUTPUT_MODE_SUBWOOFER_MODES:
            helper_snapshot = subwoofer_runtime.snapshot() if subwoofer_runtime is not None else {}
            helper_needs_sync = bool(
                request.rate_change
                or not helper_snapshot.get("active")
                or _helper_argument_sample_rate(helper_snapshot) != target_rate
                or not diagnosis.get("helper_ports")
                or not all(diagnosis.get("links", {}).values())
            )
            if helper_needs_sync:
                if request.operation in {"measurement-entry", "measurement-restore"}:
                    await _sync_subwoofer_runtime(
                        audio_overview=overview,
                        reason=f"coordinator-{request.operation}",
                        _rate_lock_held=True,
                    )
                else:
                    # Preserve the original adapter call shape for normal
                    # playback transitions; measurement/output-mode are the
                    # only operations that must carry an explicit target
                    # overview through this Coordinator-owned path.
                    await _sync_subwoofer_runtime(
                        reason=f"coordinator-{request.operation}",
                    )
                helper_rebuilt = True
            # EasyEffects may recreate its direct front links after a preset
            # action.  Reconcile them after helper setup without restarting
            # either process.
            if helper_needs_sync or not diagnosis.get("links_complete"):
                await _coordinator_reconcile_subwoofer_links_only()
                links_reconciled = True
        elif not diagnosis.get("links_complete"):
            await _repair_stereo_output_links_once(diagnosis)
            links_reconciled = True

    final = await _playback_graph_diagnosis(
        overview,
        target_rate=target_rate,
        require_source=False,
    )
    if not final.get("links_complete"):
        _log_playback_graph_diagnosis(
            final,
            target_rate=target_rate,
            reason=f"coordinator-{request.operation}",
            detail=request.detail,
        )
        raise RuntimeError(
            "Coordinator effects/helper graph did not reach the canonical topology"
        )
    return {
        "dsp_reinitialized": preset_reloaded,
        "preset_reloaded": preset_reloaded,
        "helper_rebuilt": helper_rebuilt,
        "links_reconciled": links_reconciled,
        "graph_complete": True,
        "graph_signature": final.get("signature"),
    }


async def _playback_graph_links_complete(
    audio_overview: dict | None = None,
    *,
    source: str | None = None,
    target_rate: int | None = None,
    require_source: bool = False,
) -> bool:
    """Read the canonical graph snapshot and return its commit predicate."""
    diagnosis = await _playback_graph_diagnosis(
        audio_overview,
        source=source,
        target_rate=target_rate,
        require_source=require_source,
    )
    return diagnosis["links_complete"]


def _capture_playback_state_before_measurement(
    playback_context: Mapping[str, Any] | None = None,
):
    """Save playback state before measurement starts.

    After measurement at 48 kHz, the active playback stream (paused/playing) is
    stale at the wrong sample rate. This capture enables a controlled restart
    on resume instead of a simple unpause.

    Stores source, url/path, id, title, expected_rate, position, and paused
    flag so the controlled restart can restore the user's exact spot.

    Side-effect: also fills _radio_state_before_measurement / radio_stream_stale
    so the existing radio-specific path in toggle_playback keeps working.
    """
    global _playback_state_before_measurement, _radio_state_before_measurement
    if measurement_sr_session is not None and measurement_sr_session._playback_captured:
        return
    _playback_state_before_measurement = None
    _radio_state_before_measurement = None
    context = playback_context if isinstance(playback_context, Mapping) else {}
    if context.get("source") == "spotify":
        spotify_state = dict(context.get("spotify") or {})
        target_track = dict(context.get("target_track") or {})
        spotify_identity = str(
            context.get("target_url")
            or target_track.get("id")
            or target_track.get("url")
            or spotify_state.get("trackId")
            or spotify_state.get("url")
            or ""
        ).strip()
        if not spotify_identity:
            return
        track_id = target_track.get("id") or spotify_state.get("trackId")
        was_playing = bool(context.get("should_play"))
        track_info = dict(target_track)
        track_info.update({
            "source": "spotify",
            "id": track_id,
            "url": spotify_identity,
            "title": track_info.get("title") or spotify_state.get("title"),
            "artist": track_info.get("artist") or spotify_state.get("artist"),
            "sample_rate_hz": SPOTIFY_PREARM_SAMPLE_RATE_HZ,
        })
        _playback_state_before_measurement = {
            "source": "spotify",
            "track_info": track_info,
            "url": spotify_identity,
            "path": spotify_identity,
            "current_file": None,
            "id": track_id,
            "spotify_identity": spotify_identity,
            "title": track_info.get("title"),
            "expected_rate": SPOTIFY_PREARM_SAMPLE_RATE_HZ,
            "position": 0.0,
            "was_paused": not was_playing,
            "was_playing": was_playing,
            "intent_generation": playback_intent_generation,
        }
        if measurement_sr_session is not None:
            measurement_sr_session._playback_captured = True
        logger.info(
            "PLAYBACK-CAPTURE-DIAG Spotify state captured before measurement: id=%s expected_rate=%s",
            track_id or spotify_identity,
            SPOTIFY_PREARM_SAMPLE_RATE_HZ,
        )
        return
    if not current_track_info:
        return
    source = current_track_info.get("source")
    if source not in {"radio", "local"}:
        return
    if not player_instance or not player_instance._running:
        return
    state = player_instance.state
    current_file = state.get("current_file") or ""
    if not current_file or state.get("ended"):
        return

    # Resolve expected rate
    expected_rate = None
    if source == "radio":
        # Read the radio stream's actual decoded sample rate from mpv
        try:
            expected_rate = _get_player_audio_samplerate()
        except Exception as exc:
            logger.warning("PLAYBACK-CAPTURE-DIAG could not read player audio samplerate: %s", exc)
        if not isinstance(expected_rate, int) or expected_rate <= 0:
            expected_rate = None
    elif source == "local":
        # For local tracks, use the track metadata sample rate
        expected_rate = current_track_info.get("sample_rate_hz")
    if not isinstance(expected_rate, int) or expected_rate <= 0:
        logger.warning(
            "PLAYBACK-CAPTURE-DIAG no expected_rate available, skipping capture: source=%s url=%s",
            source, current_track_info.get("url", ""),
        )
        return

    saved_state = {
        "source": source,
        "track_info": dict(current_track_info),
        "url": current_track_info.get("url", ""),
        "path": current_track_info.get("path", ""),
        "current_file": current_file,
        "id": current_track_info.get("id", ""),
        "title": current_track_info.get("title", ""),
        "expected_rate": expected_rate,
        "position": float(state.get("position", 0) or 0),
        "was_paused": bool(state.get("paused")),
        "was_playing": not state.get("paused") and not state.get("ended"),
        "intent_generation": playback_intent_generation,
    }
    _playback_state_before_measurement = saved_state
    # Mirror to radio-specific state for backwards compat with radio branch
    if source == "radio":
        _radio_state_before_measurement = {
            "track_info": dict(current_track_info),
            "url": saved_state["url"],
            "id": saved_state["id"],
            "title": saved_state["title"],
            "expected_rate": expected_rate,
            "position": saved_state["position"],
        }
    logger.info(
        "PLAYBACK-CAPTURE-DIAG state captured before measurement: source=%s url=%s id=%s "
        "expected_rate=%s position=%.2f was_paused=%s was_playing=%s",
        source, saved_state["url"], saved_state["id"],
        expected_rate, saved_state["position"], saved_state["was_paused"], saved_state["was_playing"],
    )
    if measurement_sr_session is not None:
        measurement_sr_session._playback_captured = True


async def _measurement_entry_preflight(measurement_rate: int = MEASUREMENT_DEFAULT_SAMPLE_RATE) -> None:
    """Validate the guarded measurement state before creating a sweep job."""
    coordinator = playback_transition_coordinator
    if coordinator is None:
        raise RuntimeError("PlaybackTransitionCoordinator is not available for measurement entry")
    if coordinator.transition_active or coordinator.gate.failure_latched or coordinator.gate.closed:
        raise RuntimeError("measurement entry is blocked by an active or latched playback transition")

    status = dict(get_samplerate_status())
    if status.get("active_rate") != measurement_rate:
        raise RuntimeError(
            "measurement entry preflight rate mismatch: "
            f"expected={measurement_rate} actual={status.get('active_rate')}"
        )
    if status.get("force_rate") not in {None, 0, measurement_rate}:
        raise RuntimeError(
            "measurement entry preflight force-rate mismatch: "
            f"expected={measurement_rate} actual={status.get('force_rate')}"
        )

    diagnosis = await _playback_graph_diagnosis(
        target_rate=measurement_rate,
        require_source=False,
    )
    if not diagnosis.get("links_complete"):
        session_active = bool(
            measurement_sr_session is not None
            and getattr(measurement_sr_session, "active", False)
        )
        reconciler = getattr(
            coordinator,
            "reconcile_measurement_session",
            None,
        )
        if session_active and callable(reconciler):
            reconciled_state = await reconciler(
                target_rate=measurement_rate,
                initial_graph=diagnosis,
            )
            if not isinstance(reconciled_state, Mapping) or not (
                reconciled_state.get("graph_complete") is True
                or reconciled_state.get("links_complete") is True
            ):
                raise RuntimeError(
                    "measurement session reconcile did not confirm a complete canonical graph"
                )
        else:
            _log_playback_graph_diagnosis(
                diagnosis,
                target_rate=measurement_rate,
                reason="measurement-entry-preflight",
                detail="before-sweep",
            )
            raise RuntimeError("measurement entry preflight found an incomplete canonical graph")

    if measurement_store is not None:
        playback_target = measurement_store._resolve_playback_target()
        if not isinstance(playback_target, Mapping) or not playback_target.get("target_name"):
            raise RuntimeError("measurement entry preflight found no playback target")
        playback_route = measurement_store._build_measurement_playback_route(
            "fxroute-measure-preflight",
            playback_target,
        )
        if not isinstance(playback_route, Mapping) or not playback_route.get("route"):
            raise RuntimeError("measurement entry preflight found no playback route")
        target_name = str(playback_route.get("playback_target_name") or "").strip()
        list_ports = getattr(measurement_store, "_list_pw_ports", None)
        if not target_name or not callable(list_ports):
            raise RuntimeError("measurement entry preflight could not inspect playback route ports")
        try:
            ports = set(str(port) for port in list_ports(target_name))
        except Exception as exc:
            raise RuntimeError(f"measurement entry preflight could not read playback route ports: {exc}") from exc
        required_ports = {
            f"{target_name}:playback_FL",
            f"{target_name}:playback_FR",
        }
        if not required_ports.issubset(ports):
            raise RuntimeError(
                "measurement entry preflight playback route is incomplete: "
                f"target={target_name} missing={sorted(required_ports - ports)}"
            )


async def _measurement_restore_snapshot_matches_current_intent(
    snapshot: Mapping[str, Any] | None,
) -> bool:
    """Return whether a captured playback snapshot is still user-intended."""
    if not snapshot:
        return False
    expected_source = str(snapshot.get("source") or "")
    if expected_source == "spotify":
        try:
            spotify_state = await get_spotify_ui_state()
        except Exception:
            return False
        expected_identities = _spotify_snapshot_identity_values(snapshot)
        live_identities = _spotify_identity_values(spotify_state)
        status = str(spotify_state.get("status") or "").strip().lower()
        if status not in {"playing", "paused"}:
            return False
        if not expected_identities or not live_identities.intersection(expected_identities):
            return False
        expected_generation = snapshot.get("intent_generation")
        return not (
            isinstance(expected_generation, int)
            and expected_generation != playback_intent_generation
        )

    expected_track = snapshot.get("track_info") or {}
    expected_id = snapshot.get("id") or expected_track.get("id")
    expected_url = (
        snapshot.get("url")
        or snapshot.get("path")
        or expected_track.get("url")
        or expected_track.get("path")
    )
    live_track = current_track_info or {}
    if str(live_track.get("source") or "") != expected_source:
        return False
    if expected_id is not None and live_track.get("id") != expected_id:
        return False
    if expected_url and live_track.get("url") != expected_url:
        return False

    state = dict(player_instance.state if player_instance else {})
    current_file = state.get("current_file")
    if not current_file or state.get("ended"):
        return False
    expected_file = snapshot.get("current_file") or expected_url
    if expected_file and current_file != expected_file:
        return False
    expected_generation = snapshot.get("intent_generation")
    if (
        isinstance(expected_generation, int)
        and expected_generation != playback_intent_generation
    ):
        return False
    return True


def _resolve_measurement_start_sample_rate() -> int:
    if measurement_store is not None and hasattr(measurement_store, "_resolve_measurement_sample_rate"):
        try:
            sample_rate = int(measurement_store._resolve_measurement_sample_rate())
            if sample_rate > 0:
                return sample_rate
        except Exception as exc:
            logger.warning("Measurement sample-rate resolution failed, using 48000 Hz fallback: %s", exc)
    return 48_000


async def _wait_for_selected_output_effective_rate(expected_rate: int, timeout_ms: int = 3000) -> tuple[bool, dict]:
    last_overview: dict = {}
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while time.monotonic() <= deadline:
        last_overview = get_audio_output_overview()
        output_mode = last_overview.get("output_mode") or {}
        effective_rate = output_mode.get("effective_output_rate")
        if isinstance(effective_rate, int) and effective_rate == expected_rate:
            return True, last_overview
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
    if not last_overview:
        last_overview = get_audio_output_overview()
    return False, last_overview


def _audio_output_overview_with_effective_rate(overview: dict, effective_rate: int) -> dict:
    """Thin wrapper: overview payload normalization lives in samplerate (REFACTOR-003)."""
    return samplerate.audio_output_overview_with_effective_rate(overview, effective_rate)


def _pulse_suspend_sink_for_samplerate(output_key: str, reason: str) -> None:
    if not output_key:
        return
    for suspend in ("1", "0"):
        completed = subprocess.run(
            ["pactl", "suspend-sink", output_key, suspend],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.5,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise RuntimeError(stderr or f"pactl suspend-sink {output_key} {suspend} failed")
        if suspend == "1":
            time.sleep(0.3)
    logger.info("Measurement samplerate sink pulse completed: output=%s reason=%s", output_key, reason)


def _measurement_helper_snapshot_summary(snapshot: dict | None) -> dict:
    """Thin wrapper: helper snapshot summary lives in samplerate (REFACTOR-003)."""
    return samplerate.measurement_helper_snapshot_summary(snapshot)


def _log_22_measurement_sweep_config(config: SubwooferRuntimeConfig, snapshot: dict | None) -> None:
    if config.output_mode not in OUTPUT_MODE_SUBWOOFER_22_MODES:
        return
    snapshot = snapshot or {}
    logger.info(
        "2.2 measurement sweep config: mode=%s sub1_alignment_ms=%.2f sub2_alignment_ms=%.2f "
        "derived_main=%.2f derived_sub1=%.2f derived_sub2=%.2f helper_pid=%s helper_args=%s",
        config.output_mode,
        config.sub_alignment_ms,
        config.sub2_alignment_ms,
        config.derived_main_delay_ms,
        config.derived_sub1_delay_ms,
        config.derived_sub2_delay_ms,
        snapshot.get("helper_pid"),
        snapshot.get("helper_args"),
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


def _contains_link(text: str, source: str, target: str) -> bool:
    if source not in text or target not in text:
        return False
    direct = f"{source} -> {target}"
    reverse_pw_link_io = f"{target}\n  |<- {source}"
    forward_pw_link_io = f"{source}\n  |-> {target}"
    return direct in text or reverse_pw_link_io in text or forward_pw_link_io in text


async def _ensure_mpv_to_easyeffects_links(timeout_ms: int = 1500) -> bool:
    """Ensure only the newly-created MPV stream is connected to EasyEffects.

    Radio-to-radio switches keep the existing EasyEffects output graph. MPV's
    PipeWire stream is the part that is recreated by ``loadfile`` and may need
    an idempotent direct link while mpv is still paused.
    """
    expected = (
        ("mpv:output_FL", "easyeffects_sink:playback_FL"),
        ("mpv:output_FR", "easyeffects_sink:playback_FR"),
    )
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while True:
        try:
            links_text = await _run_pw_link_command("-l")
            missing = [(source, target) for source, target in expected if not _contains_link(links_text, source, target)]
            if not missing:
                logger.info("Radio handoff MPV->EasyEffects links complete")
                return True
            for source, target in missing:
                logger.info("Radio handoff repairing MPV->EasyEffects link: %s -> %s", source, target)
                await _connect_ports((source,), target)
        except Exception as exc:
            if time.monotonic() >= deadline:
                logger.warning("Radio handoff MPV->EasyEffects link repair failed: %s", exc)
                return False
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
    try:
        links_text = await _run_pw_link_command("-l")
        return all(_contains_link(links_text, source, target) for source, target in expected)
    except Exception:
        return False


async def _dump_21_runtime_state(label: str, ui_state: dict | None = None) -> dict:
    overview = get_audio_output_overview()
    output_mode = overview.get("output_mode") or {}
    output_key = str(output_mode.get("effective_output_key") or "").strip()
    samplerate_status = get_samplerate_status()
    snapshot = subwoofer_runtime.snapshot() if subwoofer_runtime is not None else {}
    helper_pid = snapshot.get("helper_pid")
    helper_alive = False
    helper_cmdline = ""
    if helper_pid:
        ps_result = await asyncio.to_thread(_run_debug_command, ["ps", "-p", str(helper_pid), "-o", "pid=,args="], 1.5)
        helper_alive = ps_result.get("returncode") == 0 and bool(ps_result.get("stdout", "").strip())
        helper_cmdline = ps_result.get("stdout", "").strip()
    else:
        pgrep_result = await asyncio.to_thread(_run_debug_command, ["pgrep", "-af", "fxroute_21_passthrough"], 1.5)
        helper_cmdline = pgrep_result.get("stdout", "").strip()

    pw_links = await asyncio.to_thread(_run_debug_command, ["pw-link", "-l"], 2.0)
    link_text = pw_links.get("stdout", "")
    ee_left = "ee_soe_output_level:output_FL"
    ee_right = "ee_soe_output_level:output_FR"
    helper_in_left = "fxroute_21_stage1:input_L"
    helper_in_right = "fxroute_21_stage1:input_R"
    helper_out_1 = "fxroute_21_stage1:output_1"
    helper_out_2 = "fxroute_21_stage1:output_2"
    helper_out_3 = "fxroute_21_stage1:output_3"
    helper_out_4 = "fxroute_21_stage1:output_4"
    hw_fl = f"{output_key}:playback_FL" if output_key else ""
    hw_fr = f"{output_key}:playback_FR" if output_key else ""
    hw_rl = f"{output_key}:playback_RL" if output_key else ""
    hw_rr = f"{output_key}:playback_RR" if output_key else ""
    links = {
        "ee_to_helper_left": _contains_link(link_text, ee_left, helper_in_left),
        "ee_to_helper_right": _contains_link(link_text, ee_right, helper_in_right),
        "helper_main_left_to_hw": bool(hw_fl) and _contains_link(link_text, helper_out_1, hw_fl),
        "helper_main_right_to_hw": bool(hw_fr) and _contains_link(link_text, helper_out_2, hw_fr),
        "helper_sub_left_to_hw": bool(hw_rl) and _contains_link(link_text, helper_out_3, hw_rl),
        "helper_sub_right_to_hw": bool(hw_rr) and _contains_link(link_text, helper_out_4, hw_rr),
        "direct_ee_left_to_hw": bool(hw_fl) and _contains_link(link_text, ee_left, hw_fl),
        "direct_ee_right_to_hw": bool(hw_fr) and _contains_link(link_text, ee_right, hw_fr),
    }
    links["sub_output_channel_linked"] = links["helper_sub_left_to_hw"] or links["helper_sub_right_to_hw"]
    links["ee_to_helper_present"] = links["ee_to_helper_left"] and links["ee_to_helper_right"]
    links["helper_main_to_hw_present"] = links["helper_main_left_to_hw"] and links["helper_main_right_to_hw"]
    links["helper_sub_to_hw_present"] = links["helper_sub_left_to_hw"] and links["helper_sub_right_to_hw"]
    links["direct_ee_to_hw_present"] = links["direct_ee_left_to_hw"] or links["direct_ee_right_to_hw"]

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


def _build_measurement_audio_output_context() -> dict:
    """Build audio_output_context metadata for measurement saves."""
    context: dict = {}
    try:
        overview = get_audio_output_overview()
        output_mode = overview.get("output_mode") or {}
        mode = str(output_mode.get("mode", "stereo") or "stereo")
        if mode in OUTPUT_MODE_SUBWOOFER_MODES:
            config = SubwooferRuntimeConfig.from_overview(overview)
            snapshot = subwoofer_runtime.snapshot() if subwoofer_runtime is not None else {}
            context["output_mode"] = mode
            context["output_key"] = config.output_key
            context["output_label"] = config.output_label
            context["output_channels"] = config.output_channels
            context["sample_rate_hz"] = config.sample_rate
            context["crossover_frequency_hz"] = config.crossover_frequency_hz
            context["crossover_type"] = "LR24"
            context["main_highpass_enabled"] = config.main_highpass_enabled
            context["sub_level_db"] = config.sub_level_db
            context["sub_alignment_ms"] = config.sub_alignment_ms
            context["derived_main_delay_ms"] = config.derived_main_delay_ms
            context["derived_sub_delay_ms"] = config.derived_sub_delay_ms
            context["derived_sub1_delay_ms"] = config.derived_sub1_delay_ms
            context["derived_sub2_delay_ms"] = config.derived_sub2_delay_ms
            context["sub_polarity"] = config.sub_polarity
            context["sub2_level_db"] = config.sub2_level_db
            context["sub2_alignment_ms"] = config.sub2_alignment_ms
            context["sub2_polarity"] = config.sub2_polarity
            context["runtime_active"] = snapshot.get("active")
            context["helper_pid"] = snapshot.get("helper_pid")
        else:
            context["output_mode"] = "stereo"
    except Exception:
        logger.warning("Failed to build audio output measurement context", exc_info=True)
        context["output_mode"] = "unknown"
    return context


async def _sync_subwoofer_runtime_for_measurement_sweep(measurement_rate: int) -> None:
    if subwoofer_runtime is None:
        return None
    overview = get_audio_output_overview()
    output_mode = overview.get("output_mode") or {}
    if output_mode.get("mode") not in OUTPUT_MODE_SUBWOOFER_MODES:
        return None
    mode_num = "2.2 Stereo Bass" if output_mode.get("mode") == OUTPUT_MODE_SUBWOOFER_22_STEREO else "2.2" if output_mode.get("mode") == OUTPUT_MODE_SUBWOOFER_22 else "2.1"
    output_key = str(output_mode.get("effective_output_key") or "").strip()

    samplerate_status = get_samplerate_status()
    previous_force_rate = samplerate_status.get("force_rate")
    previous_active_rate = samplerate_status.get("active_rate")
    before = subwoofer_runtime.snapshot()
    logger.info(
        "%s measurement pre-arm starting: measurement_rate=%s samplerate_before=%s helper_before=%s",
        mode_num,
        measurement_rate,
        json.dumps(
            {
                "active_rate": previous_active_rate,
                "force_rate": previous_force_rate,
            },
            sort_keys=True,
        ),
        json.dumps(_measurement_helper_snapshot_summary(before), sort_keys=True),
    )
    logger.info(
        "%s measurement samplerate session pre-arm: target_rate=%s active_rate=%s force_rate=%s",
        mode_num,
        measurement_rate,
        previous_active_rate,
        previous_force_rate,
    )
    if previous_active_rate != measurement_rate:
        _pulse_suspend_sink_for_samplerate(output_key, "measurement-pre-arm")

    overview = _audio_output_overview_with_effective_rate(get_audio_output_overview(), measurement_rate)
    await _sync_subwoofer_runtime(overview, reason="measurement-pre-arm")

    aligned, overview = await _wait_for_selected_output_effective_rate(measurement_rate, timeout_ms=3500)
    if not aligned:
        output_mode = overview.get("output_mode") or {}
        effective_rate = output_mode.get("effective_output_rate")
        raise RuntimeError(
            f"{mode_num} measurement pre-arm failed: selected output did not reach "
            f"{measurement_rate} Hz before sweep start (effective_rate={effective_rate})"
        )

    await _sync_subwoofer_runtime(overview, reason="measurement-pre-arm")
    after = subwoofer_runtime.snapshot()
    samplerate_after = get_samplerate_status()
    after_config = after.get("config") or {}
    runtime_config = SubwooferRuntimeConfig.from_overview(overview)
    helper_rate = after_config.get("sample_rate")
    if not after.get("active") or helper_rate != measurement_rate:
        raise RuntimeError(
            f"{mode_num} measurement pre-arm failed: helper did not settle at "
            f"{measurement_rate} Hz before sweep start (active={after.get('active')} sample_rate={helper_rate})"
        )

    logger.info(
        "%s measurement helper pre-armed before sweep: target_rate=%s helper_rate_before=%s helper_rate_after=%s "
        "helper_pid=%s samplerate_after=%s helper_after=%s",
        mode_num,
        measurement_rate,
        (before.get("config") or {}).get("sample_rate"),
        helper_rate,
        after.get("helper_pid"),
        json.dumps(
            {
                "active_rate": samplerate_after.get("active_rate"),
                "force_rate": samplerate_after.get("force_rate"),
            },
            sort_keys=True,
        ),
        json.dumps(_measurement_helper_snapshot_summary(after), sort_keys=True),
    )
    _log_22_measurement_sweep_config(runtime_config, after)
    return None


async def _unregister_measurement_job_after_completion(job_id: str, generation: int) -> None:
    logger.info("Measurement session job watcher started: job_id=%s generation=%s", job_id, generation)
    for _ in range(300):
        await asyncio.sleep(0.5)
        if measurement_store is None:
            break
        try:
            job = measurement_store.get_job(job_id)
        except Exception as exc:
            logger.info("Measurement session job watcher stopped: job_id=%s job_lookup_failed=%s", job_id, exc)
            break
        status = str(job.get("status") or "")
        if status in {"completed", "failed", "cancelled"}:
            break
    if measurement_sr_session is not None and measurement_sr_session.generation == generation:
        await measurement_sr_session.unregister_manual_job(job_id)
    elif measurement_sr_session is not None:
        logger.info(
            "Measurement session stale job watcher ignored: job_id=%s watcher_generation=%s current_generation=%s",
            job_id,
            generation,
            measurement_sr_session.generation,
        )


async def _sync_subwoofer_runtime_at_rate(target_rate: int, *, _rate_lock_held: bool = False) -> None:
    """Re-sync through the central live-rate helper path after a rate transition."""
    global subwoofer_runtime
    if subwoofer_runtime is None:
        logger.info(
            "Subwoofer runtime measurement release re-sync skipped: subwoofer_runtime_missing=true target_rate=%s",
            target_rate,
        )
        return
    logger.info("Subwoofer runtime measurement release re-sync requested: raw_target_rate=%s", target_rate)
    overview = get_audio_output_overview()
    output_mode = overview.get("output_mode") or {}
    if output_mode.get("mode") not in OUTPUT_MODE_SUBWOOFER_MODES:
        logger.info(
            "Subwoofer runtime measurement release re-sync skipped: api_mode=%s target_rate=%s",
            output_mode.get("mode"), target_rate,
        )
        return

    # target_rate is diagnostic/stale-check information only. Do not inject it
    # into an overview: the central sync reads the authoritative rate again
    # under the sample-rate lock immediately before deciding to start.
    if target_rate > 0:
        selected_aligned, _ = await _wait_for_selected_output_effective_rate(target_rate, timeout_ms=3500)
        sink_aligned = await _wait_for_samplerate_alignment(target_rate, timeout_ms=3500)
        if not selected_aligned or not sink_aligned:
            logger.warning(
                "Subwoofer runtime measurement release re-sync deferred: target_rate=%s "
                "selected_output_aligned=%s sink_aligned=%s",
                target_rate, selected_aligned, sink_aligned,
            )
            return
    await _sync_subwoofer_runtime(
        reason="measurement-release", _rate_lock_held=_rate_lock_held,
    )
    await asyncio.sleep(0.5)
    await _sync_subwoofer_runtime(
        reason="measurement-release-settle", _rate_lock_held=_rate_lock_held,
    )
    runtime_snapshot = subwoofer_runtime.snapshot()
    try:
        samplerate_status = get_samplerate_status()
    except Exception:
        samplerate_status = {}
    logger.info(
        "Subwoofer runtime measurement release re-sync verified: target_rate=%s authoritative_rate=%s "
        "hardware_sink_rate=%s active=%s helper_pid=%s helper_rate=%s",
        target_rate,
        _authoritative_sample_rate(samplerate_status),
        samplerate_status.get("active_rate") if isinstance(samplerate_status, dict) else None,
        runtime_snapshot.get("active"),
        runtime_snapshot.get("helper_pid"),
        _helper_argument_sample_rate(runtime_snapshot),
    )
    asyncio.create_task(_repair_subwoofer_runtime_inputs_after_measurement_release(target_rate))


async def _repair_subwoofer_runtime_inputs_after_measurement_release(target_rate: int) -> None:
    """Repair delayed EasyEffects -> helper input loss after measurement release.

    The UI path can briefly restore the helper graph successfully and then lose
    only the EasyEffects output_FL/FR -> helper input_L/R links a few seconds
    later. This repair is intentionally narrow: it does not change mode,
    sample-rate policy, helper output links, QC, or measurement analysis.
    """
    if subwoofer_runtime is None:
        return
    for delay in (2.0, 5.0, 9.0):
        await asyncio.sleep(delay)
        overview = get_audio_output_overview()
        output_mode = overview.get("output_mode") or {}
        if output_mode.get("mode") not in OUTPUT_MODE_SUBWOOFER_MODES:
            logger.info(
                "Measurement release input repair skipped: api_mode=%s target_rate=%s",
                output_mode.get("mode"),
                target_rate,
            )
            return
        state = await _dump_21_runtime_state(
            "backend-release-input-repair-check",
            {"target_rate": target_rate, "delay_s": delay},
        )
        links = state.get("links") or {}
        if links.get("ee_to_helper_present") and not links.get("direct_ee_to_hw_present"):
            continue
        logger.info(
            "Measurement release input repair applying: target_rate=%s delay_s=%.1f links=%s",
            target_rate,
            delay,
            json.dumps(links, sort_keys=True),
        )
        track = dict(current_track_info or {})
        if track:
            await _request_coordinated_recovery(track, "measurement-release-delayed-link-repair")
        else:
            logger.info("Measurement release input repair skipped: no active track")
        await _dump_21_runtime_state(
            "backend-release-input-repair-after",
            {"target_rate": target_rate, "delay_s": delay},
        )


def _reset_samplerate_drift_observation() -> None:
    global samplerate_drift_signature, samplerate_drift_readbacks
    samplerate_drift_signature = None
    samplerate_drift_readbacks = 0


async def _observe_playback_samplerate_drift() -> None:
    """Observe a stable source/MPV/hardware-rate mismatch without mutating playback."""
    global samplerate_drift_signature, samplerate_drift_readbacks

    # The Coordinator and the measurement session own all rate mutations.  A
    # readback captured during either operation is not evidence of a settled
    # playback drift and must not start a competing repair.
    if _playback_transition_is_active() or _is_measurement_window_open() or (
        measurement_sr_session is not None and measurement_sr_session.active
    ):
        _reset_samplerate_drift_observation()
        return

    track = dict(current_track_info or {})
    source = str(track.get("source") or "")
    if source not in {"local", "radio"}:
        _reset_samplerate_drift_observation()
        return

    state = dict(player_instance.state if player_instance else {})
    current_file = state.get("current_file")
    expected_url = str(track.get("url") or "")
    if (
        not current_file
        or state.get("ended")
        or (expected_url and current_file != expected_url)
    ):
        _reset_samplerate_drift_observation()
        return

    # Read all rate domains as one observation.  The track rate is the last
    # successful Coordinator context, MPV audio-params is the live source
    # truth, and the PipeWire values are the current hardware readback.
    track_rate = track.get("sample_rate_hz")
    if not isinstance(track_rate, int) or track_rate <= 0:
        track_rate = _coordinator_target_rate(source, track)
    actual_rate = _get_player_audio_samplerate()
    try:
        samplerate_status = get_samplerate_status()
    except Exception:
        samplerate_status = {}
    active_rate = samplerate_status.get("active_rate") if isinstance(samplerate_status, dict) else None
    force_rate = samplerate_status.get("force_rate") if isinstance(samplerate_status, dict) else None
    if (
        not isinstance(track_rate, int)
        or track_rate <= 0
        or not isinstance(actual_rate, int)
        or actual_rate <= 0
        or not isinstance(active_rate, int)
        or active_rate <= 0
    ):
        _reset_samplerate_drift_observation()
        return

    healthy = (
        track_rate == actual_rate
        and active_rate == actual_rate
        and (force_rate is None or force_rate == 0 or force_rate == actual_rate)
    )
    if healthy:
        _reset_samplerate_drift_observation()
        return

    signature = (
        source,
        expected_url or str(track.get("id") or ""),
        str(current_file),
        track_rate,
        actual_rate,
        active_rate,
        force_rate,
    )
    if signature == samplerate_drift_signature:
        samplerate_drift_readbacks += 1
    else:
        samplerate_drift_signature = signature
        samplerate_drift_readbacks = 1

    # One readback can be a transient MPV property update.  Require the same
    # source and the same mismatch on a later watcher pass before requesting
    # recovery.
    if samplerate_drift_readbacks <= 1:
        return

    diagnosis = {
        "signature": (
            f"samplerate:{source}:{expected_url or track.get('id')}:"
            f"track={track_rate}:mpv={actual_rate}:active={active_rate}:force={force_rate}"
        ),
        "expected_rate": track_rate,
        "track_rate": track_rate,
        "actual_rate": actual_rate,
        "mpv_rate": actual_rate,
        "hardware_rate": active_rate,
        "force_rate": force_rate,
    }
    _reset_samplerate_drift_observation()
    logger.warning(
        "Stable playback samplerate drift observed; requesting Coordinator recovery: "
        "source=%s url=%s track=%s mpv=%s active=%s force=%s",
        source,
        expected_url,
        track_rate,
        actual_rate,
        active_rate,
        force_rate,
    )
    recovery_track = dict(track)
    # MPV is the authoritative source-rate readback for this repair.  Keep
    # the watcher read-only by passing a copy; the Coordinator updates the
    # committed track context only after a successful recovery commit.
    recovery_track["sample_rate_hz"] = actual_rate
    await _request_coordinated_recovery(
        recovery_track,
        "samplerate-drift-watcher",
        reload_source=True,
        diagnosis=diagnosis,
    )


async def _subwoofer_runtime_link_watch_loop() -> None:
    while True:
        await asyncio.sleep(2.0)
        try:
            await _observe_playback_samplerate_drift()
            if subwoofer_runtime is None:
                continue
            overview = get_audio_output_overview()
            output_mode = overview.get("output_mode") or {}
            if output_mode.get("mode") not in OUTPUT_MODE_SUBWOOFER_MODES:
                continue
            if playback_transition_coordinator is not None and playback_transition_coordinator.transition_active:
                continue
            track = dict(current_track_info or {})
            if not track:
                continue
            source = str(track.get("source") or "")
            target_rate = _coordinator_target_rate(source, track)
            diagnosis = await _playback_graph_diagnosis(
                overview,
                source=source,
                target_rate=target_rate,
                require_source=True,
            )
            if diagnosis.get("links_complete"):
                continue
            logger.info(
                "Subwoofer link watcher observed incomplete canonical graph; requesting Coordinator action: "
                "bypass_only=%s helper_active=%s helper_rate=%s direct_bypass=%s signature=%s",
                diagnosis.get("bypass_only"),
                diagnosis.get("helper_active"),
                diagnosis.get("helper_rate"),
                diagnosis.get("direct_ee_to_hw_present"),
                diagnosis.get("signature"),
            )
            await _request_coordinated_recovery(
                track,
                "subwoofer-link-watcher",
                graph_only=bool(diagnosis.get("bypass_only")),
                diagnosis=diagnosis,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Subwoofer link watch repair failed: %s", exc)


def _get_player_audio_samplerate() -> Optional[int]:
    global player_instance
    if not player_instance or not player_instance._running:
        return None
    try:
        audio_params = player_instance.get_property("audio-params")
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
    state = player_instance.state if player_instance else {}
    if rate and (not expected_url or state.get("current_file") == expected_url):
        return rate
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while time.monotonic() <= deadline:
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
        state = player_instance.state if player_instance else {}
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
        if transition_generation != playback_transition_generation:
            logger.info(
                "Radio post-load rate wait aborted: stale transition "
                "generation=%s current=%s",
                transition_generation,
                playback_transition_generation,
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




async def _sync_easyeffects_preset_for_playback_samplerate(
    *,
    sample_rate_hz: Optional[int],
    reason: str,
    detail: str = "",
) -> None:
    global easyeffects_manager
    if not easyeffects_manager or not isinstance(sample_rate_hz, int) or sample_rate_hz <= 0:
        return

    active_preset = easyeffects_manager.get_active_preset()
    if not active_preset or active_preset in easyeffects_manager.EXCLUDED_GLOBAL_EXTRAS_PRESETS:
        return

    logger.info(
        "Syncing EasyEffects preset for playback samplerate: preset=%s sample_rate=%s reason=%s detail=%s",
        active_preset,
        sample_rate_hz,
        reason,
        detail,
    )
    easyeffects_manager.load_preset(active_preset, convolver_sample_rate_hz=sample_rate_hz)
    status = easyeffects_manager.get_status()
    await manager.broadcast({"type": "easyeffects", "data": status})




def _can_use_native_local_queue(tracks: list[dict]) -> bool:
    """Return whether MPV can own one already-safe homogeneous playlist."""
    if len(tracks) <= 1:
        return False
    rates = []
    for track in tracks:
        if track.get("source", "local") != "local" or not str(track.get("url") or "").strip():
            return False
        rate = track.get("sample_rate_hz")
        if not isinstance(rate, int) or rate <= 0:
            return False
        rates.append(rate)
    return len(set(rates)) == 1


def _native_queue_request_fields() -> dict[str, Any]:
    """Snapshot native-queue metadata for a Coordinator request."""
    if playback_queue_mode != "native_mpv" or not _can_use_native_local_queue(playback_queue):
        return {}
    start_index = playback_queue_index if playback_queue_index >= 0 else 0
    return {
        "native_queue": tuple(dict(item) for item in playback_queue),
        "native_queue_index": start_index,
        "native_queue_loop": bool(playback_queue_loop),
        # Queue order is already concrete in playback_queue.  Keep the field
        # explicit for request compatibility, but never ask MPV to reshuffle it.
        "native_queue_shuffle": False,
    }


def _prepare_local_queue(track_id: str, queue_track_ids: Optional[list[str]] = None, shuffle: bool = False, loop: bool = False, *, reshuffle: bool = True):
    global playback_queue, playback_queue_original, playback_queue_index, playback_queue_mode, playback_queue_loop, playback_queue_shuffle, single_track_loop
    playback_queue = []
    playback_queue_original = []
    playback_queue_index = -1
    playback_queue_mode = "app_replace"
    playback_queue_loop = False
    playback_queue_shuffle = False
    single_track_loop = False
    tracks = library_scanner.get_tracks()
    tracks_by_id = {track.id: track for track in tracks}

    selected_ids = []
    requested_ids = queue_track_ids if queue_track_ids else [track_id]
    for candidate in requested_ids:
        if candidate in tracks_by_id and candidate not in selected_ids:
            selected_ids.append(candidate)
    if track_id in tracks_by_id and track_id not in selected_ids:
        selected_ids.insert(0, track_id)

    ordered_tracks = [tracks_by_id[selected_id].to_dict() for selected_id in selected_ids]
    if not ordered_tracks:
        raise HTTPException(status_code=404, detail="Track not found")

    original_tracks = [dict(track) for track in ordered_tracks]

    if shuffle and reshuffle and len(ordered_tracks) > 1:
        current_track = next((track for track in ordered_tracks if track.get("id") == track_id), ordered_tracks[0])
        remaining = [track for track in ordered_tracks if track.get("id") != current_track.get("id")]
        random.shuffle(remaining)
        ordered_tracks = [current_track] + remaining

    playback_queue = ordered_tracks if len(ordered_tracks) > 1 else []
    playback_queue_original = original_tracks if len(original_tracks) > 1 else []
    # A homogeneous local queue is safe to hand to MPV only after the
    # Coordinator has committed the common rate/DSP/graph/gate state.  The
    # request carries the immutable queue snapshot; the mode becomes visible
    # as native only after that transition commits.
    playback_queue_mode = "native_mpv" if _can_use_native_local_queue(ordered_tracks) else "app_replace"
    playback_queue_loop = bool(loop and len(ordered_tracks) > 1)
    playback_queue_shuffle = bool(shuffle and len(ordered_tracks) > 1)
    single_track_loop = bool(loop and len(ordered_tracks) == 1)
    playback_queue_index = -1

    if playback_queue:
        for index, item in enumerate(playback_queue):
            if item.get("id") == track_id:
                return _sync_track_context_from_queue_index(index)
        return _sync_track_context_from_queue_index(0)

    return ordered_tracks[0]




async def _load_queue_track(index: int, *, transition_reason: str = "queue navigation") -> bool:
    global playback_queue_index
    if len(playback_queue) <= 1 or index < 0 or index >= len(playback_queue):
        return False
    next_track = dict(playback_queue[index])
    target_url = str(next_track.get("url") or "")
    if not target_url:
        _clear_playback_queue()
        return False
    source = str(next_track.get("source") or "local")
    target_rate = _coordinator_target_rate(source, next_track)
    native_fields = _native_queue_request_fields()
    native_jump = playback_queue_mode == "native_mpv"
    request = TransitionRequest(
        operation="queue",
        source=source,
        target_rate=target_rate,
        target_url=target_url,
        target_track=next_track,
        should_play=True,
        rate_change=_coordinator_rate_change(target_rate),
        reload_source=not native_jump,
        detail=transition_reason,
        **native_fields,
        native_queue_jump=index if native_jump else None,
    )
    try:
        result = await _run_coordinated_transition(request)
    except PlaybackTransitionFailure as exc:
        raise _transition_error_http(exc) from exc
    if source in {"local", "radio"} and isinstance(result.target_rate, int) and result.target_rate > 0:
        next_track["sample_rate_hz"] = result.target_rate
        if 0 <= index < len(playback_queue):
            playback_queue[index]["sample_rate_hz"] = result.target_rate
    playback_queue_index = index
    _commit_coordinated_track(next_track, source=source)
    return True


async def _advance_playback_queue(*, transition_reason: str = "queue advance") -> bool:
    global playback_queue_index
    if len(playback_queue) <= 1:
        return False
    next_index = playback_queue_index + 1
    if next_index >= len(playback_queue):
        if playback_queue_mode == "native_mpv":
            if playback_queue_loop:
                next_index = 0
            else:
                return False
        else:
            manual_shuffle_wrap = playback_queue_shuffle and transition_reason.startswith("manual queue next")
            if playback_queue_loop or manual_shuffle_wrap:
                if playback_queue_shuffle:
                    current_index = playback_queue_index if 0 <= playback_queue_index < len(playback_queue) else 0
                    current_track_id = (playback_queue[current_index] or {}).get("id")
                    current_track = dict(playback_queue[current_index])
                    remaining = [dict(track) for track in playback_queue if track.get("id") != current_track_id]
                    random.shuffle(remaining)
                    playback_queue[:] = [current_track] + remaining
                    playback_queue_index = 0
                    next_index = 1 if len(playback_queue) > 1 else 0
                else:
                    next_index = 0
            else:
                _clear_playback_queue()
                return False
    return await _load_queue_track(next_index, transition_reason=transition_reason)


async def _rewind_playback_queue(*, transition_reason: str = "queue rewind") -> bool:
    if len(playback_queue) <= 1:
        return False
    prev_index = playback_queue_index - 1
    if prev_index < 0:
        return False
    return await _load_queue_track(prev_index, transition_reason=transition_reason)


async def _set_queue_shuffle(enabled: bool) -> bool:
    global playback_queue, playback_queue_original, playback_queue_index
    global playback_queue_shuffle, current_track_info, last_track_info
    if len(playback_queue) <= 1:
        playback_queue_shuffle = False
        return False
    current_index = playback_queue_index if 0 <= playback_queue_index < len(playback_queue) else 0
    current_track = dict(playback_queue[current_index])
    current_track_id = current_track.get("id")
    current_track_url = current_track.get("url")

    if enabled:
        remaining = [
            dict(track)
            for index, track in enumerate(playback_queue)
            if index != current_index
        ]
        random.shuffle(remaining)
        target_queue = [current_track] + remaining
        target_index = 0
    elif playback_queue_original:
        target_queue = [dict(track) for track in playback_queue_original]
        target_index = next(
            (
                index
                for index, track in enumerate(target_queue)
                if (
                    current_track_id is not None
                    and track.get("id") == current_track_id
                )
                or (
                    current_track_id is None
                    and current_track_url
                    and track.get("url") == current_track_url
                )
            ),
            min(current_index, len(target_queue) - 1),
        )
    else:
        target_queue = [dict(track) for track in playback_queue]
        target_index = current_index

    if playback_queue_mode == "native_mpv":
        # Replacing a native playlist changes the source staging boundary and
        # therefore belongs to the Coordinator.  Keep the old queue visible
        # until this gated replacement commits successfully.
        target_track = dict(target_queue[target_index])
        target_url = str(target_track.get("url") or "")
        if not target_url:
            return False
        player_state = player_instance.state if player_instance else {}
        should_play = bool(
            player_state.get("playing")
            and not player_state.get("paused")
            and not player_state.get("ended")
        )
        target_rate = _coordinator_target_rate("local", target_track)
        try:
            result = await _run_coordinated_transition(TransitionRequest(
                operation="queue",
                source="local",
                target_rate=target_rate,
                target_url=target_url,
                target_track=target_track,
                should_play=should_play,
                rate_change=_coordinator_rate_change(target_rate),
                reload_source=True,
                detail="queue-shuffle-on" if enabled else "queue-shuffle-off",
                native_queue=tuple(target_queue),
                native_queue_index=target_index,
                native_queue_loop=bool(playback_queue_loop),
                native_queue_shuffle=False,
            ))
        except PlaybackTransitionFailure:
            raise

        committed_rate = getattr(result, "target_rate", None)
        if isinstance(committed_rate, int) and committed_rate > 0:
            for track in target_queue:
                track["sample_rate_hz"] = committed_rate
        playback_queue = target_queue
        playback_queue_index = target_index
        playback_queue_shuffle = bool(enabled)
        current_track_info = dict(target_queue[target_index])
        last_track_info = dict(target_queue[target_index])
        return True

    playback_queue = target_queue
    playback_queue_index = target_index
    playback_queue_shuffle = bool(enabled)
    return True


def _set_queue_loop(enabled: bool) -> bool:
    global playback_queue_loop, single_track_loop
    has_local_track = bool(current_track_info and current_track_info.get("source") == "local")
    if not has_local_track:
        playback_queue_loop = False
        single_track_loop = False
        return False
    if len(playback_queue) > 1:
        playback_queue_loop = bool(enabled)
        single_track_loop = False
        if playback_queue_mode == "native_mpv" and _player_is_running():
            set_loop_playlist = getattr(player_instance, "set_loop_playlist", None)
            if callable(set_loop_playlist):
                set_loop_playlist(playback_queue_loop)
        return True
    single_track_loop = bool(enabled)
    playback_queue_loop = False
    return True


def _sync_active_local_queue_selection(queue_track_ids: Optional[list[str]] = None, shuffle: bool = False, loop: bool = False) -> dict:
    global current_track_info, last_track_info, playback_queue_mode
    current_track = dict(current_track_info or {})
    if current_track.get("source") != "local" or not current_track.get("id"):
        raise HTTPException(status_code=409, detail="Local playback is not active")

    player_state = player_instance.state if player_instance else {}
    if not player_state.get("current_file") or player_state.get("ended"):
        raise HTTPException(status_code=409, detail="Nothing is currently loaded to update")

    # A queue-selection change must not leave MPV's old native future entries
    # alive behind the new app-side queue metadata. Keep the current source,
    # then explicitly return to app-owned queue navigation below.
    if playback_queue_mode == "native_mpv":
        _reduce_native_mpv_playlist_to_current()
        _reset_mpv_loop_state()

    track_info = _prepare_local_queue(
        current_track["id"],
        queue_track_ids,
        shuffle=shuffle,
        loop=loop,
        reshuffle=False,
    )
    current_track_info = track_info
    last_track_info = track_info

    if len(playback_queue) > 1:
        playback_queue_mode = "app_replace"

    if player_instance and player_instance._running:
        _reset_mpv_loop_state()

    return build_playback_payload(player_state)


def ensure_local_source_volume() -> None:
    global player_instance
    if not player_instance or not player_instance._running:
        return
    try:
        player_instance.set_volume(100)
    except Exception as exc:
        logger.warning("Failed to pin MPV source volume to 100%%: %s", exc)


def get_output_volume_safe(default: int = 100) -> int:
    if easyeffects_manager:
        try:
            loudness = easyeffects_manager.load_global_extras().get("loudness", {})
            if loudness.get("enabled"):
                volume_db = float(loudness.get("params", {}).get("volumeDb", 0.0))
                return easyeffects_manager.loudness_percent_from_db(volume_db)
        except Exception:
            logger.warning("Failed to read Loudness volume, falling back to system volume", exc_info=True)
    try:
        return get_output_volume()
    except Exception as exc:
        logger.warning("Failed to read output volume, using fallback %s: %s", default, exc)
        return default


def _set_canonical_output_volume(volume: float | int) -> dict[str, Any]:
    """Apply the one UI-volume contract for local, radio and Spotify.

    Loudness owns the attenuation when enabled, therefore the physical system
    master remains at 100%.  Without Loudness the existing FXRoute system
    volume curve remains authoritative.
    """
    requested = max(0, min(100, int(round(float(volume)))))
    extras = easyeffects_manager.load_global_extras() if easyeffects_manager else {}
    loudness = extras.get("loudness") if isinstance(extras, dict) else {}
    if isinstance(loudness, dict) and loudness.get("enabled") and easyeffects_manager:
        volume_db = easyeffects_manager.loudness_db_from_percent(requested)
        volume_result = easyeffects_manager.set_loudness_volume_db(volume_db)
        set_output_volume(100)
        return {
            "volume": requested,
            "loudnessVolumeDb": float(
                volume_result["extras"]["loudness"]["params"]["volumeDb"]
            ),
            "loudness_enabled": True,
        }
    return {
        "volume": set_output_volume(requested),
        "loudnessVolumeDb": None,
        "loudness_enabled": False,
    }


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
        cover_available = bool(library_scanner and _track_cover_available(track_id))
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


def build_playback_payload(state: Optional[dict] = None) -> dict:
    global current_track_info, easyeffects_manager, player_instance, peak_monitor
    playback_state = dict(state or (player_instance.state if player_instance else {}))
    source_volume = playback_state.get("volume") if isinstance(playback_state.get("volume"), (int, float)) else None
    if current_track_info and current_track_info.get("source") in {"local", "radio"}:
        playback_state["source_volume"] = int(round(float(source_volume))) if source_volume is not None else None
    elif source_volume is not None:
        playback_state["source_volume"] = int(round(float(source_volume)))
    playback_state["volume"] = get_output_volume_safe(int(round(float(source_volume))) if source_volume is not None else 100)
    # Radio: hide stale track from UI when mpv has no active stream.
    # Prevents UI showing a resumable station when the stream connection
    # is dead and mpv is idle (current_file=None, ended=True).
    _effective_track = current_track_info
    if _effective_track and _effective_track.get("source") == "radio":
        cur_file = playback_state.get("current_file")
        if not cur_file or playback_state.get("ended"):
            _effective_track = None
    playback_state["current_track"] = _playback_track_with_artwork_fields(_effective_track)
    playback_state["queue"] = _queue_payload()
    playback_state["footer_owner"] = _get_authoritative_footer_owner(playback_state=playback_state)

    live_title = None
    if player_instance and current_track_info and current_track_info.get("source") == "radio":
        metadata = player_instance.get_metadata() if playback_state.get("current_file") else {}
        title = (metadata.get("icy-title") or metadata.get("title") or "").strip()
        if title:
            live_title = title
        playback_state["metadata"] = metadata

    playback_state["live_title"] = live_title
    playback_state["output_peak_warning"] = peak_monitor.snapshot() if peak_monitor else {
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
        playback_state["transition"] = transition_status
        gate = transition_status.get("gate") or {}
        if transition_status.get("active") or gate.get("closed") or gate.get("failure_latched"):
            # A physical source may still report playing while FXRoute owns a
            # safety mute, or while the coordinator has not yet returned a
            # committed result.  Do not expose that transient as committed
            # normal playback to the UI; the structured transition status is
            # the authoritative state until the gate is released.
            playback_state["playing"] = False
            playback_state["safe_muted"] = True
            playback_state["transition_status"] = (
                "failure-latched"
                if gate.get("failure_latched")
                else "transitioning" if transition_status.get("active") else "safe-muted"
            )

    # Keep playback/status payloads lightweight. EasyEffects has dedicated
    # endpoints and websocket updates, and pulling full EasyEffects status here
    # can stall frequent /api/status polling during playback.
    return playback_state


async def on_peak_monitor_change(snapshot: dict):
    await manager.broadcast({"type": "playback_peak_warning", "data": snapshot})


async def sync_peak_monitor_for_playback_state(
    state: dict,
    transition_generation: int | None = None,
):
    global peak_monitor_playback_armed, peak_monitor, peak_monitor_transition_lock, peak_monitor_context_signature, current_track_info
    if not peak_monitor:
        return
    if transition_generation is None:
        transition_generation = playback_transition_generation
    if not _playback_transition_context_is_current(transition_generation):
        return
    if peak_monitor_transition_lock is None:
        peak_monitor_transition_lock = asyncio.Lock()
    async with peak_monitor_transition_lock:
        if not _playback_transition_context_is_current(transition_generation):
            return
        is_active_playback = _is_local_playback_active(state)
        source = (current_track_info or {}).get("source") or "unknown"
        state_matches_track = _playback_state_matches_track(state, current_track_info)
        if is_active_playback and not state_matches_track and peak_monitor_playback_armed:
            logger.info(
                "Skipping peak monitor resync during unsettled player transition: source=%s state_file=%s track_url=%s track_id=%s",
                source,
                state.get("current_file"),
                (current_track_info or {}).get("url"),
                (current_track_info or {}).get("id"),
            )
            return
        desired_signature = f"player:{source}:{state.get('current_file') or ''}" if is_active_playback else None

        if is_active_playback:
            # Resume from pause/inactive with same source:
            # only restart the peak monitor — do NOT reload the EasyEffects
            # preset or repair the output graph, which causes an audible crack.
            if (
                not peak_monitor_playback_armed
                and peak_monitor_context_signature == desired_signature
            ):
                peak_monitor_playback_armed = True
                logger.info(
                    "Repairing peak monitor links after pause (same source, relink only): %s",
                    desired_signature,
                )
                # Peak monitor process was kept running but PipeWire links are
                # dropped during pause. Repair links without restarting the
                # pw-record process to avoid audible cracks.
                relinked = await peak_monitor.relink()
                if not relinked:
                    logger.warning(
                        "Peak monitor relink failed; falling back to full restart: %s",
                        desired_signature,
                    )
                    await peak_monitor.restart()
                await manager.broadcast({"type": "playback_peak_warning", "data": peak_monitor.snapshot()})
            elif peak_monitor_context_signature != desired_signature:
                peak_monitor_playback_armed = True
                peak_monitor_context_signature = desired_signature
                if not _playback_transition_context_is_current(transition_generation):
                    return
                logger.info(
                    "Restarting peak monitor on committed playback context change; production graph remains coordinator-owned: %s",
                    desired_signature,
                )
                await peak_monitor.restart()
                await manager.broadcast({"type": "playback_peak_warning", "data": peak_monitor.snapshot()})
        elif (
            not is_active_playback
            and peak_monitor_playback_armed
            and str(peak_monitor_context_signature or "").startswith("player:")
        ):
            await asyncio.sleep(PEAK_MONITOR_INACTIVE_GRACE_MS / 1000)
            refreshed_player_state = player_instance.state if player_instance else {}
            if refreshed_player_state.get("current_file") and not refreshed_player_state.get("paused") and not refreshed_player_state.get("ended"):
                return
            spotify_state = await get_spotify_ui_state()
            if spotify_state.get("available") and spotify_state.get("status") == "Playing":
                return
            # Keep the peak monitor process running through pauses to avoid
            # pw-record restart + PipeWire link glitches on resume.
            # Mark as not armed so the resume path will trigger relink().
            logger.info("Peak monitor pausing (process stays alive, armed=False): signature=%s", peak_monitor_context_signature)
            peak_monitor_playback_armed = False
            # peak_monitor_context_signature is preserved for same-source resume detection.


async def sync_peak_monitor_for_spotify_state(data: dict):
    global peak_monitor_playback_armed, peak_monitor, player_instance, peak_monitor_transition_lock, peak_monitor_context_signature
    if not peak_monitor:
        return
    if peak_monitor_transition_lock is None:
        peak_monitor_transition_lock = asyncio.Lock()

    async with peak_monitor_transition_lock:
        player_state = player_instance.state if player_instance else {}
        is_spotify_playing = _is_spotify_playback_active(data)
        desired_signature = "spotify:playing" if is_spotify_playing else None

        if is_spotify_playing and (not peak_monitor_playback_armed or peak_monitor_context_signature != desired_signature):
            if _playback_transition_is_active():
                logger.info("Delaying peak monitor restart while Spotify samplerate recovery is active")
                return
            peak_monitor_playback_armed = True
            peak_monitor_context_signature = desired_signature
            logger.info(
                "Starting peak monitor for committed Spotify playback; rate/graph mutations remain coordinator-owned",
            )
            await peak_monitor.restart()
            await manager.broadcast({"type": "playback_peak_warning", "data": peak_monitor.snapshot()})
        elif (
            not is_spotify_playing
            and peak_monitor_playback_armed
            and str(peak_monitor_context_signature or "").startswith("spotify:")
        ):
            if _playback_transition_is_active():
                logger.info("Keeping peak monitor armed while Spotify samplerate recovery is active")
                return
            await asyncio.sleep(PEAK_MONITOR_INACTIVE_GRACE_MS / 1000)
            refreshed_player_state = player_instance.state if player_instance else {}
            refreshed_spotify_state = await get_spotify_ui_state()
            if _playback_transition_is_active():
                logger.info("Keeping peak monitor armed while Spotify samplerate recovery is still active")
                return
            if _is_local_playback_active(refreshed_player_state):
                return
            if _is_spotify_playback_active(refreshed_spotify_state):
                return
            logger.info("Stopping peak monitor because Spotify is no longer actively playing")
            await peak_monitor.stop()
            peak_monitor_playback_armed = False
            peak_monitor_context_signature = None
            await manager.broadcast({"type": "playback_peak_warning", "data": peak_monitor.snapshot()})


async def sync_peak_monitor_for_source_mode_state(source_overview: dict | None = None):
    global peak_monitor_playback_armed, peak_monitor, player_instance, peak_monitor_transition_lock, peak_monitor_context_signature
    if not peak_monitor:
        return
    if peak_monitor_transition_lock is None:
        peak_monitor_transition_lock = asyncio.Lock()

    async with peak_monitor_transition_lock:
        overview = source_overview or get_audio_source_overview()
        bluetooth = overview.get("bluetooth") or {}
        is_bt_streaming = bool(
            overview.get("mode") == SOURCE_MODE_BLUETOOTH_INPUT
            and bluetooth.get("state") == "streaming"
            and bluetooth.get("connected_device")
        )
        desired_signature = None
        if is_bt_streaming:
            desired_signature = f"bluetooth:{bluetooth.get('connected_device')}:{bluetooth.get('active_codec') or ''}"

        if is_bt_streaming and (not peak_monitor_playback_armed or peak_monitor_context_signature != desired_signature):
            peak_monitor_playback_armed = True
            peak_monitor_context_signature = desired_signature
            logger.info("Starting peak monitor for active Bluetooth input: %s", desired_signature)
            await peak_monitor.restart()
            await manager.broadcast({"type": "playback_peak_warning", "data": peak_monitor.snapshot()})
        elif (not is_bt_streaming) and peak_monitor_playback_armed and str(peak_monitor_context_signature or "").startswith("bluetooth:"):
            player_state = player_instance.state if player_instance else {}
            spotify_state = await get_spotify_ui_state()
            if not _is_local_playback_active(player_state) and not _is_spotify_playback_active(spotify_state):
                logger.info("Stopping peak monitor because Bluetooth input is no longer actively streaming")
                await peak_monitor.stop()
                peak_monitor_playback_armed = False
                peak_monitor_context_signature = None
                await manager.broadcast({"type": "playback_peak_warning", "data": peak_monitor.snapshot()})


async def refresh_peak_monitor_after_effects_change(reason: str = "effects-change"):
    global peak_monitor, peak_monitor_playback_armed, peak_monitor_context_signature, player_instance
    if not peak_monitor or not peak_monitor_playback_armed:
        return

    player_state = player_instance.state if player_instance else {}
    spotify_state = await get_spotify_ui_state()
    is_local_playing = bool(player_state.get("current_file") and not player_state.get("paused") and not player_state.get("ended"))
    is_spotify_playing = bool(spotify_state.get("available") and spotify_state.get("status") == "Playing")

    if not is_local_playing and not is_spotify_playing:
        return

    logger.info("Refreshing peak monitor after %s", reason)
    peak_monitor_context_signature = None
    await asyncio.sleep(PEAK_MONITOR_RESTART_SETTLE_MS / 1000)

    if is_spotify_playing:
        await sync_peak_monitor_for_spotify_state(spotify_state)
    elif is_local_playing:
        await sync_peak_monitor_for_playback_state(player_state)


async def _run_peak_monitor_refresh_after_effects_change(reason: str, timeout: float = 4.0):
    try:
        await asyncio.wait_for(refresh_peak_monitor_after_effects_change(reason), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Timed out refreshing peak monitor after %s", reason)
    except Exception as e:
        logger.warning("Failed refreshing peak monitor after %s: %s", reason, e)


def schedule_peak_monitor_refresh_after_effects_change(reason: str = "effects-change"):
    asyncio.create_task(_run_peak_monitor_refresh_after_effects_change(reason))



async def _radio_reconnect_after_delay(
    track_info: dict,
    attempt: int,
    transition_generation: int,
) -> None:
    global radio_reconnect_task
    try:
        await asyncio.sleep(RADIO_RECONNECT_DELAY_SECONDS)
        expected_url = (track_info or {}).get("url")
        if not expected_url:
            return
        if not player_instance or not player_instance._running:
            return
        if not _playback_transition_context_is_current(transition_generation):
            return
        if not current_track_info or current_track_info.get("source") != "radio" or current_track_info.get("url") != expected_url:
            return
        state = player_instance.state
        if state.get("current_file") and not state.get("ended"):
            return
        logger.info("Reconnecting radio stream after unexpected end: station=%s attempt=%s/%s", track_info.get("title") or track_info.get("id"), attempt, RADIO_RECONNECT_MAX_ATTEMPTS)
        if not _playback_transition_context_is_current(transition_generation):
            return
        await _request_coordinated_recovery(
            track_info,
            "radio-reconnect",
            reload_source=True,
        )
    except Exception as e:
        logger.warning("Radio stream reconnect failed: %s", e)
    finally:
        radio_reconnect_task = None


def _schedule_radio_reconnect_if_needed(state: dict) -> None:
    global radio_reconnect_task, radio_reconnect_attempts, radio_reconnect_url, radio_reconnect_active_since
    track_info = current_track_info or {}
    track_url = track_info.get("url")
    if track_info.get("source") != "radio" or not track_url:
        return

    if state.get("current_file") and not state.get("ended"):
        if radio_reconnect_url != track_url:
            radio_reconnect_url = track_url
            radio_reconnect_attempts = 0
            radio_reconnect_active_since = time.monotonic()
        elif not radio_reconnect_active_since:
            radio_reconnect_active_since = time.monotonic()
        elif radio_reconnect_attempts and time.monotonic() - radio_reconnect_active_since >= 30.0:
            radio_reconnect_attempts = 0
        return

    radio_reconnect_active_since = 0.0
    if not (state.get("ended") and not state.get("current_file")):
        return

    if radio_reconnect_url != track_url:
        radio_reconnect_url = track_url
        radio_reconnect_attempts = 0
    if radio_reconnect_attempts >= RADIO_RECONNECT_MAX_ATTEMPTS:
        logger.warning("Radio stream ended and reconnect limit reached: station=%s url=%s", track_info.get("title") or track_info.get("id"), track_url)
        return
    if radio_reconnect_task and not radio_reconnect_task.done():
        return

    radio_reconnect_attempts += 1
    radio_reconnect_task = asyncio.create_task(
        _radio_reconnect_after_delay(
            dict(track_info),
            radio_reconnect_attempts,
            playback_transition_generation,
        )
    )


# Callback functions
def _mark_player_state_authoritative(state: dict | None) -> None:
    global latest_player_state_seq_seen
    seq = (state or {}).get("_seq")
    if isinstance(seq, int):
        latest_player_state_seq_seen = max(latest_player_state_seq_seen, seq)


async def on_player_state_change(state: dict):
    global queue_advancing, playback_queue_index, current_track_info, last_track_info, queue_transition_target_url, latest_player_state_seq_seen
    callback_generation = playback_transition_generation
    seq = state.get("_seq")
    if isinstance(seq, int):
        if seq < latest_player_state_seq_seen:
            return
        latest_player_state_seq_seen = seq

    if queue_transition_target_url:
        current_file = state.get("current_file")
        if current_file == queue_transition_target_url and not state.get("ended"):
            queue_transition_target_url = None

    # Once a homogeneous queue has been committed, MPV owns natural playlist
    # boundaries.  A path/playlist-pos event only updates application context;
    # it must never start another rate, DSP, graph or gate transition.
    if (
        playback_queue_mode == "native_mpv"
        and len(playback_queue) > 1
        and not _playback_transition_is_active()
        and not state.get("ended")
        and state.get("current_file")
    ):
        # MPV's playlist-pos is the native (possibly shuffled) playlist
        # position, not FXRoute's stable queue index. The current URL is the
        # authoritative cross-context identity; only use playlist-pos when it
        # also names that same app-side track.
        queue_index = next(
            (
                index
                for index, track in enumerate(playback_queue)
                if track.get("url") == state.get("current_file")
            ),
            None,
        )
        if queue_index is None:
            native_index = state.get("playlist_pos")
            if isinstance(native_index, int) and 0 <= native_index < len(playback_queue):
                candidate = playback_queue[native_index]
                if candidate.get("url") == state.get("current_file"):
                    queue_index = native_index
        if queue_index is not None:
            track = dict(playback_queue[queue_index])
            previous_track = current_track_info or {}
            playback_queue_index = queue_index
            current_track_info = track
            last_track_info = track
            if (
                previous_track.get("source") != track.get("source")
                or previous_track.get("id") != track.get("id")
                or previous_track.get("url") != track.get("url")
            ):
                _mark_playback_intent_changed()

    if (
        not queue_advancing
        and not queue_transition_target_url
        and state.get("ended")
        and not state.get("current_file")
        and current_track_info
        and current_track_info.get("source") == "local"
        and playback_queue_mode != "native_mpv"
    ):
        queue_advancing = True
        try:
            if len(playback_queue) > 1 and await _advance_playback_queue(transition_reason="queue auto-advance"):
                return
            if single_track_loop and current_track_info and current_track_info.get("url"):
                loop_track = dict(current_track_info)
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
                    if isinstance(result.target_rate, int) and result.target_rate > 0:
                        current_track_info["sample_rate_hz"] = result.target_rate
                except PlaybackTransitionFailure as exc:
                    logger.warning("Single-track loop transition failed: %s", exc.as_status())
                return
        finally:
            queue_advancing = False

    _schedule_radio_reconnect_if_needed(state)
    if source_transition_lock is None:
        await sync_peak_monitor_for_playback_state(state, callback_generation)
    else:
        # Serialize callback context application with explicit play handoffs.
        # A callback queued before/during a handoff observes an obsolete
        # generation after acquiring the lock and becomes a no-op.
        async with source_transition_lock:
            await sync_peak_monitor_for_playback_state(state, callback_generation)
    if not _playback_transition_context_is_current(callback_generation):
        logger.debug(
            "Discarding stale player callback after playback transition: callback_generation=%s current_generation=%s",
            callback_generation,
            playback_transition_generation,
        )
        return
    await manager.broadcast({"type": "playback", "data": build_playback_payload(state)})

async def on_download_progress(progress):
    data = progress.to_dict() if hasattr(progress, "to_dict") else progress
    await manager.broadcast({"type": "download", "data": data})

    status = (data or {}).get("status")
    if status == "complete":
        global library_scanner
        if library_scanner:
            library_scanner.refresh(force=True)
        await manager.broadcast({"type": "download_complete", "data": data})
    elif status == "error":
        await manager.broadcast({"type": "download_error", "data": data})

async def broadcast_spotify_state(data=None):
    global latest_spotify_state
    data = await get_spotify_ui_state(data)
    latest_spotify_state = data
    await sync_peak_monitor_for_spotify_state(data)
    if _is_spotify_playback_active(data):
        signature_payload = repr(_spotify_state_signature(data)).encode("utf-8", errors="replace")
        _schedule_silent_active_watch(
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
    global latest_spotify_state
    try:
        data = await get_spotify_ui_state()
        if force or _spotify_refresh_should_broadcast(data, latest_spotify_state):
            if _spotify_identity_signature(data) != _spotify_identity_signature(latest_spotify_state):
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
            latest_spotify_state = data
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Spotify metadata refresh failed (%s): %s", reason, exc)


def _schedule_spotify_state_refresh(reason: str) -> None:
    global spotify_state_refresh_task
    if spotify_state_refresh_task and not spotify_state_refresh_task.done():
        spotify_state_refresh_task.cancel()

    async def _delayed_refresh() -> None:
        await asyncio.sleep(SPOTIFY_STATE_REFRESH_DEBOUNCE_SECONDS)
        await _refresh_spotify_state_from_mpris(reason)

    spotify_state_refresh_task = asyncio.create_task(
        _delayed_refresh(),
        name="spotify-state-refresh",
    )


async def _spotify_state_poll_loop() -> None:
    logger.info("Spotify metadata poll fallback entered")
    while True:
        try:
            await _refresh_spotify_state_from_mpris("poll-fallback")
            state = latest_spotify_state or {}
            active = bool(state.get("available") and (state.get("status") == "Playing" or current_footer_owner == "spotify"))
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
    global current_footer_owner, latest_spotify_state
    current_footer_owner = "local"
    if not await _spotify_player_present():
        latest_spotify_state = {
            "available": playerctl_available(),
            "installed": spotify_installed(),
            "source": "spotify",
            "status": "Stopped",
            "footer_owner": "local",
        }
        await manager.broadcast({"type": "spotify", "data": latest_spotify_state})
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
    global player_instance, current_footer_owner, current_track_info
    current_footer_owner = "spotify"
    try:
        if player_instance and player_instance._running:
            player_instance.stop_playback()
            current_track_info = None
            await manager.broadcast({"type": "playback", "data": build_playback_payload(player_instance.state)})
            released = await _wait_for_pipewire_mpv_release()
            if not released:
                await asyncio.sleep(SOURCE_HANDOFF_SETTLE_MS / 1000)
    except Exception:
        pass


async def _run_pactl_command(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "pactl", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="ignore").strip() or f"pactl {' '.join(args)} failed")
    return stdout.decode(errors="ignore").strip()


async def _run_pw_link_command(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "pw-link", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="ignore").strip() or f"pw-link {' '.join(args)} failed")
    return stdout.decode(errors="ignore").strip()


async def _disconnect_ports(source_ports: tuple[str, ...], sink_port: str) -> None:
    for source_port in source_ports:
        try:
            await _run_pw_link_command("-d", source_port, sink_port)
            return
        except Exception:
            continue


async def _connect_ports(source_ports: tuple[str, ...], sink_port: str) -> None:
    last_exc: Exception | None = None
    for source_port in source_ports:
        try:
            await _run_pw_link_command(source_port, sink_port)
            return
        except Exception as exc:
            message = str(exc).lower()
            if "file exists" in message or "exists" in message or "already linked" in message:
                return
            last_exc = exc
    if last_exc:
        raise last_exc


async def _disconnect_external_input_source(source_name: str | None) -> None:
    normalized = (source_name or "").strip()
    if not normalized:
        return
    for channel in ("FL", "FR"):
        sink_port = f"easyeffects_sink:playback_{channel}"
        await _disconnect_ports((f"{normalized}:capture_{channel}",), sink_port)


async def _disable_external_input_loopback() -> None:
    global external_input_loopback_module_id, external_input_loopback_source_name
    previous_source = external_input_loopback_source_name
    if external_input_loopback_module_id is not None:
        try:
            await _run_pactl_command("unload-module", str(external_input_loopback_module_id))
            logger.info("Disabled legacy external-input loopback module %s", external_input_loopback_module_id)
        except Exception as exc:
            logger.warning("Failed to unload legacy external-input loopback module %s: %s", external_input_loopback_module_id, exc)
    await _disconnect_external_input_source(previous_source)
    external_input_loopback_module_id = None
    external_input_loopback_source_name = None


async def _ensure_external_input_loopback(source_name: str) -> None:
    global external_input_loopback_module_id, external_input_loopback_source_name
    normalized = (source_name or "").strip()
    if not normalized:
        raise RuntimeError("Missing source name for external-input monitoring")
    if external_input_loopback_source_name == normalized:
        return
    await _disable_external_input_loopback()
    for channel in ("FL", "FR"):
        source_port = f"{normalized}:capture_{channel}"
        sink_port = f"easyeffects_sink:playback_{channel}"
        try:
            await _connect_ports((source_port,), sink_port)
        except Exception:
            raise
    external_input_loopback_module_id = None
    external_input_loopback_source_name = normalized
    logger.info("Enabled direct external-input monitoring from %s to easyeffects_sink", normalized)


async def _sync_external_input_monitoring(source_overview: dict | None = None) -> dict:
    overview = source_overview or get_audio_source_overview()
    if overview.get("mode") != SOURCE_MODE_EXTERNAL_INPUT:
        await _disable_external_input_loopback()
        return overview
    current_input = overview.get("selected_input") or overview.get("current_input") or {}
    source_name = current_input.get("source_key") or current_input.get("name")
    if not source_name:
        await _disable_external_input_loopback()
        return overview
    await _ensure_external_input_loopback(str(source_name))
    return overview


async def _disconnect_bluetooth_input_source(source_name: str | None) -> None:
    normalized = (source_name or "").strip()
    if not normalized:
        return
    try:
        await _link_bluetooth_source_to_easyeffects(normalized, disconnect=True)
    except Exception:
        pass


async def _stop_bluetooth_audio_agent() -> None:
    global bluetooth_agent_process
    proc = bluetooth_agent_process
    bluetooth_agent_process = None
    if not proc:
        return
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


async def _ensure_bluetooth_audio_agent() -> None:
    global bluetooth_agent_process
    proc = bluetooth_agent_process
    if proc and proc.returncode is None:
        return
    agent_script = BASE_DIR / "bluez_audio_agent.py"
    bluetooth_agent_process = await asyncio.create_subprocess_exec(
        str(agent_script),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.sleep(0.4)
    if bluetooth_agent_process.returncode is not None:
        stderr = await bluetooth_agent_process.stderr.read()
        bluetooth_agent_process = None
        raise RuntimeError((stderr or b"BlueZ audio agent exited immediately").decode(errors="ignore").strip())


async def _clear_bluetooth_input_monitoring_links() -> None:
    global bluetooth_input_source_name
    previous_source = bluetooth_input_source_name
    bluetooth_input_source_name = None
    await _disconnect_bluetooth_input_source(previous_source)


async def _link_bluetooth_source_to_easyeffects(source_name: str, disconnect: bool = False) -> None:
    normalized = (source_name or "").strip()
    if not normalized:
        return
    failures: list[str] = []
    for channel in ("FL", "FR"):
        sink_port = f"easyeffects_sink:playback_{channel}"
        source_ports = (f"{normalized}:capture_{channel}", f"{normalized}:output_{channel}")
        try:
            if disconnect:
                await _disconnect_ports(source_ports, sink_port)
            else:
                await _connect_ports(source_ports, sink_port)
        except Exception as exc:
            failures.append(f"{channel}: {exc}")

    if failures:
        raise RuntimeError("failed to link ports: " + "; ".join(failures))


async def _disable_bluetooth_input_monitoring() -> None:
    await _clear_bluetooth_input_monitoring_links()
    await _stop_bluetooth_audio_agent()
    try:
        disconnected = disconnect_connected_bluetooth_audio_sources()
        if disconnected:
            logger.info("Disconnected Bluetooth audio source devices while leaving bluetooth-input mode: %s", ", ".join(disconnected))
    except Exception as exc:
        logger.warning("Failed to disconnect Bluetooth audio source devices: %s", exc)


async def _ensure_bluetooth_input_loopback(source_name: str) -> None:
    global bluetooth_input_source_name
    normalized = (source_name or "").strip()
    if not normalized:
        raise RuntimeError("Missing Bluetooth source name for monitoring")
    if bluetooth_input_source_name == normalized:
        return
    await _clear_bluetooth_input_monitoring_links()
    await _link_bluetooth_source_to_easyeffects(normalized)
    bluetooth_input_source_name = normalized
    logger.info("Enabled Bluetooth input monitoring from %s to easyeffects_sink", normalized)


async def _sync_bluetooth_input_monitoring(source_overview: dict | None = None) -> dict:
    overview = source_overview or get_audio_source_overview()
    if overview.get("mode") != SOURCE_MODE_BLUETOOTH_INPUT:
        await _disable_bluetooth_input_monitoring()
        try:
            set_bluetooth_receiver_enabled(False)
        except Exception as exc:
            logger.warning("Failed to disable Bluetooth receiver mode: %s", exc)
        return overview

    bt_state = overview.get("bluetooth") or {}
    if not bt_state.get("selectable"):
        raise RuntimeError("Bluetooth input is not currently available")

    await _ensure_bluetooth_audio_agent()
    if not bt_state.get("discoverable") or not bt_state.get("pairable"):
        set_bluetooth_receiver_enabled(True)
    bt_overview = get_bluetooth_audio_overview()
    receiver_session = bt_overview.get("receiver_session") or {}
    source_name = receiver_session.get("source_name")
    if not source_name:
        await _clear_bluetooth_input_monitoring_links()
        return get_audio_source_overview()

    await _ensure_bluetooth_input_loopback(str(source_name))
    return get_audio_source_overview()


def _overview_sample_rate(overview: dict | None) -> int | None:
    """Thin wrapper: overview rate extraction lives in samplerate (REFACTOR-003)."""
    return samplerate.overview_sample_rate(overview)


def _authoritative_sample_rate(status: dict | None) -> int | None:
    """Thin wrapper: authoritative rate extraction lives in samplerate (REFACTOR-003)."""
    return samplerate.authoritative_sample_rate(status)


def _helper_argument_sample_rate(snapshot: dict | None) -> int | None:
    """Thin wrapper: helper --rate argument extraction lives in samplerate (REFACTOR-003)."""
    return samplerate.helper_argument_sample_rate(snapshot)


async def _sync_subwoofer_runtime(
    audio_overview: dict | None = None,
    *,
    reason: str = "unspecified",
    _rate_lock_held: bool = False,
    target_overview: dict | None = None,
) -> dict:
    """Synchronize the native helper from one live, lock-protected rate.

    An overview passed by a transition/release caller is only a stale-check
    token. The actual helper config is rebuilt after the final live PipeWire
    read, so a delayed caller cannot restart a helper with its old target.
    """
    global subwoofer_runtime
    overview_was_supplied = audio_overview is not None
    overview = audio_overview or get_audio_output_overview()

    if subwoofer_runtime is None:
        return overview

    requested_rate = _overview_sample_rate(overview) if overview_was_supplied else None

    async def _sync_locked() -> dict:
        try:
            samplerate_status = get_samplerate_status()
        except Exception as exc:
            logger.warning(
                "Subwoofer runtime sync skipped: authoritative samplerate unavailable reason=%s error=%s",
                reason, exc,
            )
            return overview

        authoritative_rate = _authoritative_sample_rate(samplerate_status)
        if authoritative_rate is None:
            logger.warning(
                "Subwoofer runtime sync skipped: authoritative samplerate missing reason=%s requested_rate=%s",
                reason, requested_rate,
            )
            return overview

        sink_rate = samplerate_status.get("active_rate")
        if sink_rate != authoritative_rate:
            logger.info(
                "Subwoofer runtime sync deferred until sink reaches authoritative rate: "
                "reason=%s requested_rate=%s authoritative_rate=%s hardware_sink_rate=%s",
                reason, requested_rate, authoritative_rate, sink_rate,
            )
            return overview

        current_overview = target_overview or audio_overview or get_audio_output_overview()
        current_mode = current_overview.get("output_mode") or {}
        if current_mode.get("mode") == OUTPUT_MODE_STEREO:
            # Leave the subwoofer graph in the same ordered transition path as
            # every other runtime mode change.  The helper owns removal of its
            # links, stopping the process, and restoration of the direct
            # EasyEffects -> hardware front links.  Checking the stereo graph
            # before this sync mistakes the expected subwoofer graph for a
            # broken stereo graph and can trigger a needless EE restart while
            # the system master is intentionally at 100% for Loudness.
            stereo_config = SubwooferRuntimeConfig.from_overview(current_overview)
            await subwoofer_runtime.sync(stereo_config)
            await _ensure_stereo_easyeffects_output_graph(current_overview)
            logger.info("Subwoofer runtime sync: output_mode=%s; stereo path restored", OUTPUT_MODE_STEREO)
            return current_overview
        if current_mode.get("mode") not in OUTPUT_MODE_SUBWOOFER_MODES:
            return current_overview
        if requested_rate is not None and requested_rate != authoritative_rate:
            logger.info(
                "Subwoofer runtime sync stale; helper restart suppressed: reason=%s requested_rate=%s authoritative_rate=%s",
                reason, requested_rate, authoritative_rate,
            )
            return overview
        current_overview = _audio_output_overview_with_effective_rate(
            current_overview, authoritative_rate,
        )
        pre_start_status = get_samplerate_status()
        pre_start_rate = _authoritative_sample_rate(pre_start_status)
        pre_start_sink_rate = pre_start_status.get("active_rate")
        if pre_start_rate != authoritative_rate or pre_start_sink_rate != authoritative_rate:
            logger.info(
                "Subwoofer runtime sync stale immediately before helper start; restart suppressed: "
                "reason=%s requested_rate=%s authoritative_rate=%s pre_start_rate=%s pre_start_sink_rate=%s",
                reason, requested_rate, authoritative_rate, pre_start_rate, pre_start_sink_rate,
            )
            return current_overview
        current_overview = _audio_output_overview_with_effective_rate(
            current_overview, pre_start_rate,
        )
        config = SubwooferRuntimeConfig.from_overview(current_overview)
        # This is the final rate check in the existing sample-rate critical
        # section, immediately before runtime.sync can launch the helper.
        final_status = get_samplerate_status()
        final_rate = _authoritative_sample_rate(final_status)
        if final_rate != config.sample_rate:
            logger.info(
                "Subwoofer runtime sync stale at helper-start gate; restart suppressed: "
                "reason=%s requested_rate=%s config_rate=%s final_rate=%s",
                reason, requested_rate, config.sample_rate, final_rate,
            )
            return current_overview
        await subwoofer_runtime.sync(config)

        runtime_snapshot = subwoofer_runtime.snapshot()
        helper_rate = _helper_argument_sample_rate(runtime_snapshot)
        try:
            samplerate_after = get_samplerate_status()
        except Exception:
            samplerate_after = {}
        sink_rate = samplerate_after.get("active_rate") if isinstance(samplerate_after, dict) else None
        mode_num = "2.2 Stereo Bass" if config.output_mode == OUTPUT_MODE_SUBWOOFER_22_STEREO else "2.2" if config.output_mode == OUTPUT_MODE_SUBWOOFER_22 else "2.1"
        if sink_rate == authoritative_rate and helper_rate == authoritative_rate and runtime_snapshot.get("active"):
            logger.info(
                "%s runtime sync verified: reason=%s requested_rate=%s authoritative_rate=%s "
                "hardware_sink_rate=%s helper_pid=%s helper_rate=%s runtime_active=%s output_mode=%s",
                mode_num, reason, requested_rate, authoritative_rate, sink_rate,
                runtime_snapshot.get("helper_pid"), helper_rate,
                runtime_snapshot.get("active"), config.output_mode,
            )
        else:
            logger.warning(
                "%s runtime sync not verified; triple-rate match missing: reason=%s "
                "requested_rate=%s authoritative_rate=%s hardware_sink_rate=%s helper_pid=%s "
                "helper_rate=%s runtime_active=%s output_mode=%s",
                mode_num, reason, requested_rate, authoritative_rate, sink_rate,
                runtime_snapshot.get("helper_pid"), helper_rate,
                runtime_snapshot.get("active"), config.output_mode,
            )
        return current_overview

    if _rate_lock_held or measurement_sr_session is None:
        return await _sync_locked()
    async with measurement_sr_session.lock:
        return await _sync_locked()


def _with_subwoofer_derived_delays(overview: dict) -> dict:
    output_mode = overview.get("output_mode") or {}
    if output_mode.get("mode") in OUTPUT_MODE_SUBWOOFER_22_MODES:
        config = SubwooferRuntimeConfig.from_overview(overview)
        overview["output_mode"] = {
            **output_mode,
            "derived_main_delay_ms": config.derived_main_delay_ms,
            "derived_sub1_delay_ms": config.derived_sub1_delay_ms,
            "derived_sub2_delay_ms": config.derived_sub2_delay_ms,
        }
    return overview


async def _ensure_stereo_easyeffects_output_graph(audio_overview: dict | None = None) -> None:
    if easyeffects_manager is None:
        return
    overview = audio_overview or get_audio_output_overview()
    output_mode = overview.get("output_mode") or {}
    if output_mode.get("mode") != OUTPUT_MODE_STEREO:
        return
    output_key = str(output_mode.get("effective_output_key") or "").strip()
    if not output_key or output_key == "easyeffects_sink":
        return
    try:
        result = await asyncio.to_thread(easyeffects_manager.ensure_stereo_output_graph, output_key)
        if result.get("recovered"):
            logger.warning(
                "Recovered Stereo EasyEffects output graph for %s via %s",
                output_key,
                result.get("recovery"),
            )
    except Exception as exc:
        logger.warning("Stereo EasyEffects output graph guard failed for %s: %s", output_key, exc)


async def _bluetooth_input_monitor_loop() -> None:
    while True:
        try:
            overview = get_audio_source_overview()
            if overview.get("mode") == SOURCE_MODE_BLUETOOTH_INPUT:
                overview = await _sync_bluetooth_input_monitoring(overview)
                await sync_peak_monitor_for_source_mode_state(overview)
            elif bluetooth_input_source_name:
                await _disable_bluetooth_input_monitoring()
                await sync_peak_monitor_for_source_mode_state(overview)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Bluetooth input monitor loop check failed: %s", exc)
        await asyncio.sleep(3)


async def _spotify_playerctl_event_detect_check(reason: str) -> None:
    global spotify_playerctl_detect_task
    try:
        burst_delays = (0.05, 0.15, 0.30, 0.60)
        last_snapshot: tuple[object, object, object, object] | None = None
        mismatch_signature: tuple[object, int, int] | None = None
        mismatch_readbacks = 0
        for index, delay_s in enumerate(burst_delays):
            if delay_s > 0:
                await asyncio.sleep(delay_s if index == 0 else max(0.0, delay_s - burst_delays[index - 1]))
            spotify_inputs = _list_spotify_sink_inputs()
            spotify_observation = _spotify_sink_input_observation(spotify_inputs)
            spotify_identity = spotify_observation[0] if spotify_observation else None
            spotify_rate = spotify_observation[1] if spotify_observation else None
            spotify_state = await get_spotify_ui_state()
            samplerate_status = get_samplerate_status()
            sink_rate = samplerate_status.get("active_rate")
            last_snapshot = (
                spotify_state.get("status"),
                len(spotify_inputs),
                spotify_rate,
                sink_rate,
            )
            if spotify_state.get("status") == "Playing" and isinstance(spotify_rate, int) and isinstance(sink_rate, int):
                canonical_rate = SPOTIFY_PREARM_SAMPLE_RATE_HZ
                if spotify_rate != canonical_rate or sink_rate != canonical_rate:
                    current_mismatch = (spotify_identity, spotify_rate, sink_rate)
                    if current_mismatch == mismatch_signature:
                        mismatch_readbacks += 1
                    else:
                        mismatch_signature = current_mismatch
                        mismatch_readbacks = 1
                    logger.info(
                        "Spotify detect watcher mismatch probe: reason=%s probe=%s/%s stable=%s/%s "
                        "spotify_rate=%s sink_rate=%s title=%s",
                        reason,
                        index + 1,
                        len(burst_delays),
                        mismatch_readbacks,
                        SPOTIFY_SINK_INPUT_RATE_STABILITY_POLLS,
                        spotify_rate,
                        sink_rate,
                        spotify_state.get("title"),
                    )
                    if mismatch_readbacks < SPOTIFY_SINK_INPUT_RATE_STABILITY_POLLS:
                        continue
                    logger.warning(
                        "Stable Spotify samplerate mismatch after transport event; requesting Coordinator recovery: "
                        "reason=%s spotify_rate=%s sink_rate=%s title=%s",
                        reason,
                        spotify_rate,
                        sink_rate,
                        spotify_state.get("title"),
                    )
                    track = {
                        "source": "spotify",
                        "id": spotify_state.get("trackId"),
                        "url": spotify_state.get("trackId"),
                        "title": spotify_state.get("title"),
                        "artist": spotify_state.get("artist"),
                        "sample_rate_hz": SPOTIFY_PREARM_SAMPLE_RATE_HZ,
                    }
                    diagnosis = {
                        "signature": (
                            f"spotify-samplerate:{spotify_identity}:"
                            f"{spotify_rate}->{sink_rate}"
                        ),
                        "source": "spotify",
                        "actual_rate": spotify_rate,
                        "expected_rate": SPOTIFY_PREARM_SAMPLE_RATE_HZ,
                        "hardware_rate": sink_rate,
                    }
                    await _request_coordinated_recovery(
                        track,
                        f"spotify-{reason}",
                        reload_source=True,
                        diagnosis=diagnosis,
                    )
                    break
                mismatch_signature = None
                mismatch_readbacks = 0
                logger.info(
                    "Spotify detect watcher: reason=%s probe=%s/%s status=%s spotify_inputs=%s spotify_rate=%s sink_rate=%s footer_owner=%s title=%s",
                    reason,
                    index + 1,
                    len(burst_delays),
                    spotify_state.get("status"),
                    len(spotify_inputs),
                    spotify_rate,
                    sink_rate,
                    current_footer_owner,
                    spotify_state.get("title"),
                )
                break
            mismatch_signature = None
            mismatch_readbacks = 0
        else:
            if last_snapshot is not None:
                status, inputs_count, spotify_rate, sink_rate = last_snapshot
                logger.info(
                    "Spotify detect watcher final: reason=%s probes=%s status=%s spotify_inputs=%s spotify_rate=%s sink_rate=%s footer_owner=%s",
                    reason,
                    len(burst_delays),
                    status,
                    inputs_count,
                    spotify_rate,
                    sink_rate,
                    current_footer_owner,
                )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Spotify playerctl detect check failed (%s): %s", reason, exc)
    finally:
        if spotify_playerctl_detect_task and spotify_playerctl_detect_task.done():
            spotify_playerctl_detect_task = None


def _schedule_spotify_playerctl_event_detect(reason: str) -> None:
    global spotify_playerctl_detect_task, spotify_playerctl_last_trigger_at
    if spotify_playerctl_detect_task and not spotify_playerctl_detect_task.done():
        logger.debug(
            "Spotify playerctl detect event coalesced while detect/recovery task is active: reason=%s",
            reason,
        )
        return
    now = time.monotonic()
    if now - spotify_playerctl_last_trigger_at < 1.0:
        return
    spotify_playerctl_last_trigger_at = now
    spotify_playerctl_detect_task = asyncio.create_task(
        _spotify_playerctl_event_detect_check(reason),
        name="spotify-playerctl-event-detect",
    )


async def _spotify_playerctl_watch_loop() -> None:
    logger.info("Spotify playerctl watch loop entered")
    if not spotify_installed():
        logger.info("Spotify playerctl watch skipped: Spotify client not installed")
        return
    playerctl_path = shutil.which("playerctl")
    if not playerctl_path:
        logger.info("Spotify playerctl watch skipped: playerctl not available")
        return
    logger.info("Spotify playerctl watch resolved playerctl path: %s", playerctl_path)
    while True:
        proc = None
        try:
            logger.info("Spotify playerctl watch spawning follow process")
            proc = await asyncio.create_subprocess_exec(
                playerctl_path,
                "--player=spotify",
                "metadata",
                "--follow",
                "--format",
                "{{status}}|{{title}}|{{artist}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert proc.stdout is not None
            logger.info("Spotify playerctl watch started")
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="ignore").strip()
                if not text:
                    continue
                status, _, tail = text.partition("|")
                if status == "Playing":
                    _schedule_spotify_playerctl_event_detect(f"playerctl:{tail or 'playing'}")
                _schedule_spotify_state_refresh(f"playerctl:{tail or status or 'metadata'}")
            stderr = b""
            if proc.stderr:
                try:
                    stderr = await asyncio.wait_for(proc.stderr.read(), timeout=0.2)
                except Exception:
                    stderr = b""
            if proc.returncode not in (0, None):
                logger.warning("Spotify playerctl watch exited with %s: %s", proc.returncode, stderr.decode(errors="ignore").strip() or "no stderr")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Spotify playerctl watch loop failed: %s", exc)
        finally:
            await _stop_process(proc)
        await asyncio.sleep(1.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown."""
    global settings, player_instance, library_scanner, downloader, easyeffects_manager, measurement_store, measurement_sr_session, peak_monitor, subwoofer_runtime, subwoofer_runtime_link_watch_task, hardware_controller, peak_monitor_playback_armed, peak_monitor_transition_lock, peak_monitor_context_signature, easyeffects_preset_load_lock, source_transition_lock, playback_transition_coordinator, coordinator_recovery_lock, coordinator_recovery_inflight_signature, coordinator_recovery_last_signature, coordinator_last_successful_commit_id, external_input_loopback_module_id, external_input_loopback_source_name, bluetooth_input_source_name, bluetooth_monitor_task, bluetooth_agent_process, spotify_playerctl_watch_task, spotify_playerctl_detect_task, spotify_state_refresh_task, spotify_playerctl_last_trigger_at, current_source_mode, latest_spotify_state

    # Startup
    logger.info("Starting FXRoute... build_id=%s", _read_build_id())
    try:
        settings = get_settings()
        logger.info(f"Configuration loaded. MUSIC_ROOT: {settings.MUSIC_ROOT}")
        logger.info(f"Download directory: {settings.download_dir}")

        # Initialize player
        player_instance = get_player()
        try:
            player_instance.start()
            logger.info("MPV player started")
            ensure_local_source_volume()
        except MPVNotInstalledError as e:
            logger.error(f"Failed to start MPV: {e}")

        # Initialize library scanner without blocking startup on large libraries.
        library_scanner = LibraryScanner()
        library_scanner.prepare_scan_status()
        asyncio.create_task(asyncio.to_thread(library_scanner.refresh, True))
        logger.info("Library scanner initialized; initial scan running in background")

        # Initialize downloader
        downloader = Downloader()
        logger.info("Downloader initialized")

        # Initialize EasyEffects manager
        easyeffects_manager = EasyEffectsManager()
        if easyeffects_manager.load_global_extras().get("loudness", {}).get("enabled"):
            set_output_volume(100)
        logger.info("EasyEffects manager initialized")

        measurement_store = MeasurementStore()
        logger.info("Measurement store initialized: %s", measurement_store.measurements_dir)
        measurement_sr_session = MeasurementSampleRateSession()
        asyncio.create_task(measurement_sr_session.run_watchdog())
        logger.info("Measurement sample-rate session initialized")

        playback_transition_coordinator = PlaybackTransitionCoordinator(
            FxrouteTransitionRuntime(),
            gate_state_path=_playback_gate_state_path(),
        )
        coordinator_recovery_lock = asyncio.Lock()
        coordinator_recovery_inflight_signature = None
        coordinator_recovery_last_signature = None
        coordinator_last_successful_commit_id = None
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

        peak_monitor = EasyEffectsPeakMonitor(on_change=on_peak_monitor_change)
        subwoofer_runtime = Subwoofer21Runtime()
        # Clean any orphan 2.1 helpers from a previous run before syncing state
        try:
            await subwoofer_runtime._stop_orphan_helpers()
        except Exception:
            pass
        peak_monitor_playback_armed = False
        peak_monitor_transition_lock = asyncio.Lock()
        peak_monitor_context_signature = None
        easyeffects_preset_load_lock = asyncio.Lock()
        source_transition_lock = asyncio.Lock()
        latest_spotify_state = await get_spotify_ui_state()
        await sync_peak_monitor_for_spotify_state(latest_spotify_state)
        logger.info("EasyEffects output peak monitor initialized")

        try:
            applied_output = apply_persisted_audio_output_selection()
            if applied_output and applied_output.get("selected_output"):
                logger.info("Re-applied persisted audio output selection: %s", applied_output["selected_output"].get("target_label"))
            await _sync_subwoofer_runtime(applied_output or get_audio_output_overview())
            subwoofer_runtime_link_watch_task = asyncio.create_task(_subwoofer_runtime_link_watch_loop())
        except Exception as exc:
            logger.warning("Failed to re-apply persisted audio output selection: %s", exc)

        try:
            applied_source = get_audio_source_overview()
            applied_source = await _sync_external_input_monitoring(applied_source)
            applied_source = await _sync_bluetooth_input_monitoring(applied_source)
            current_source_mode = applied_source.get("mode") or SOURCE_MODE_APP_PLAYBACK
            if applied_source.get("mode") == SOURCE_MODE_EXTERNAL_INPUT:
                logger.info(
                    "Re-applied persisted external-input monitoring: %s",
                    ((applied_source.get("selected_input") or applied_source.get("current_input") or {}).get("label") or "unknown input"),
                )
            elif applied_source.get("mode") == SOURCE_MODE_BLUETOOTH_INPUT:
                logger.info("Re-applied persisted Bluetooth input mode")
        except Exception as exc:
            logger.warning("Failed to re-apply source monitoring: %s", exc)

        bluetooth_monitor_task = asyncio.create_task(_bluetooth_input_monitor_loop())
        spotify_playerctl_last_trigger_at = 0.0
        logger.info("Starting Spotify playerctl watch task")
        spotify_playerctl_watch_task = asyncio.create_task(_spotify_playerctl_watch_loop())
        logger.info("Starting Spotify metadata poll fallback task")
        spotify_state_poll_task = asyncio.create_task(_spotify_state_poll_loop())

        # Register callbacks for state changes
        player_instance.register_callbacks(on_player_state_change)
        downloader.register_callback(on_download_progress, asyncio.get_running_loop())

        logger.info("Application startup complete build_id=%s", _read_build_id())
    except Exception as e:
        logger.error(f"Startup failed: {e}")

    yield

    # Shutdown
    if subwoofer_runtime_link_watch_task:
        subwoofer_runtime_link_watch_task.cancel()
        try:
            await subwoofer_runtime_link_watch_task
        except asyncio.CancelledError:
            pass
    if player_instance:
        player_instance.stop()
        logger.info("MPV player stopped")
    if subwoofer_runtime:
        await subwoofer_runtime.stop()
        logger.info("Subwoofer runtime stopped")
    if bluetooth_monitor_task:
        bluetooth_monitor_task.cancel()
        try:
            await bluetooth_monitor_task
        except asyncio.CancelledError:
            pass
    if spotify_playerctl_watch_task:
        spotify_playerctl_watch_task.cancel()
        try:
            await spotify_playerctl_watch_task
        except asyncio.CancelledError:
            pass
    if spotify_playerctl_detect_task:
        spotify_playerctl_detect_task.cancel()
        try:
            await spotify_playerctl_detect_task
        except asyncio.CancelledError:
            pass
    if spotify_state_refresh_task:
        spotify_state_refresh_task.cancel()
        try:
            await spotify_state_refresh_task
        except asyncio.CancelledError:
            pass
    if spotify_state_poll_task:
        spotify_state_poll_task.cancel()
        try:
            await spotify_state_poll_task
        except asyncio.CancelledError:
            pass
    await _disable_bluetooth_input_monitoring()
    try:
        set_bluetooth_receiver_enabled(False)
    except Exception:
        pass
    await _disable_external_input_loopback()
    if peak_monitor:
        await peak_monitor.stop()
        logger.info("EasyEffects output peak monitor stopped")
    if hardware_controller:
        hardware_controller.close()
        logger.info("Hardware controller closed")

app = FastAPI(lifespan=lifespan)
app.include_router(radio_api_router)

# Static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

def _effective_request_scheme(request: Request) -> str:
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    if forwarded_proto:
        return forwarded_proto
    return (request.url.scheme or "http").lower()

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

def _cover_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "application/octet-stream"


ALBUM_COVER_CACHE_DIR = BASE_DIR / "media" / "cache" / "album-covers"


def _serve_cover_image(image_path: Path, size: int = 256) -> FileResponse:
    """Serve an album cover, using cached thumbnails when Pillow is available."""
    image_path = image_path.resolve()
    if not image_path.is_file():
        raise FileNotFoundError(str(image_path))

    media_type = _cover_media_type(image_path)
    try:
        from PIL import Image
    except ModuleNotFoundError:
        logger.debug("Pillow is not installed; serving original cover image for %s", image_path)
        return FileResponse(str(image_path), media_type=media_type)

    cache_key = hashlib.sha256(
        f"{image_path}:{image_path.stat().st_mtime_ns}:{size}".encode()
    ).hexdigest()[:16]
    suffix = image_path.suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
        suffix = ".jpg"
    cached = ALBUM_COVER_CACHE_DIR / f"{cache_key}{suffix}"
    if cached.is_file():
        return FileResponse(str(cached), media_type=_cover_media_type(cached))

    ALBUM_COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with Image.open(str(image_path)) as img:
        img = img.convert("RGB")
        img.thumbnail((size, size), Image.LANCZOS)
        save_kwargs = {"quality": 85} if suffix in (".jpg", ".jpeg") else {}
        # Keep the image suffix last so Pillow can infer the encoder for the
        # atomic temporary write (e.g. ``cover.tmp.jpg`` rather than
        # ``cover.jpg.tmp``).
        tmp = cached.with_name(f"{cached.stem}.tmp{cached.suffix}")
        img.save(str(tmp), **save_kwargs)
        tmp.replace(cached)
    return FileResponse(str(cached), media_type=_cover_media_type(cached))


def _folder_cover_for_track(track_path: Path) -> Optional[Path]:
    """Find a cover image in the track's folder.
    Priority: exact names (cover.jpg etc.) > any image with cover/folder/art in name.
    """
    parent = track_path.parent
    # Fast path: exact names
    for name in (
        "cover.jpg", "cover.jpeg", "cover.png", "cover.webp",
        "folder.jpg", "folder.jpeg", "folder.png", "folder.webp",
        "front.jpg", "front.jpeg", "front.png", "front.webp",
    ):
        candidate = parent / name
        if candidate.is_file():
            return candidate
    # Fallback: any image with cover/folder/front/album/art in the filename
    try:
        for f in sorted(parent.iterdir()):
            if not f.is_file():
                continue
            fl = f.name.lower()
            if any(kw in fl for kw in ("cover", "folder", "front", "album", "art")) and fl.endswith((".jpg", ".jpeg", ".png", ".webp")):
                return f
    except OSError:
        pass
    return None


def _embedded_cover_bytes(track_path: Path) -> tuple[Optional[bytes], Optional[str]]:
    try:
        audio = MutagenFile(str(track_path), easy=False)
    except Exception as exc:
        logger.debug("Cover metadata read failed for %s: %s", track_path, exc)
        return None, None
    if not audio:
        return None, None

    for picture in getattr(audio, "pictures", []) or []:
        data = getattr(picture, "data", None)
        mime = getattr(picture, "mime", None) or "image/jpeg"
        if data:
            return bytes(data), mime

    tags = getattr(audio, "tags", None)
    if not tags:
        return None, None

    for key, value in tags.items():
        if str(key).startswith("APIC"):
            data = getattr(value, "data", None)
            mime = getattr(value, "mime", None) or "image/jpeg"
            if data:
                return bytes(data), mime

    covers = tags.get("covr") if hasattr(tags, "get") else None
    if covers:
        first = covers[0]
        image_format = getattr(first, "imageformat", None)
        mime = "image/png" if image_format == 14 else "image/jpeg"
        return bytes(first), mime

    return None, None


def _cached_embedded_cover(track_id: str, track_path: Path) -> tuple[Optional[Path], Optional[str]]:
    try:
        stat = track_path.stat()
    except FileNotFoundError:
        return None, None
    cache_key = hashlib.sha256(f"{track_id}:{track_path}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")).hexdigest()
    for suffix, media_type in ((".jpg", "image/jpeg"), (".png", "image/png"), (".webp", "image/webp")):
        cached = COVER_CACHE_DIR / f"{cache_key}{suffix}"
        if cached.is_file():
            return cached, media_type

    data, mime = _embedded_cover_bytes(track_path)
    if not data:
        return None, None
    suffix = ".png" if mime == "image/png" else ".webp" if mime == "image/webp" else ".jpg"
    COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = COVER_CACHE_DIR / f"{cache_key}{suffix}"
    tmp = cached.with_suffix(cached.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(cached)
    return cached, mime or _cover_media_type(cached)


def _track_cover_available(track_id: str) -> bool:
    tracks_by_id = {track.id: track for track in library_scanner.get_tracks(refresh=False)}
    track = tracks_by_id.get(track_id)
    if not track or not track.path:
        return False
    track_path = track.path.resolve()
    if not _path_within_root(track_path, settings.MUSIC_ROOT) or not track_path.is_file():
        return False
    folder_cover = _folder_cover_for_track(track_path)
    if folder_cover and _path_within_root(folder_cover.resolve(), settings.MUSIC_ROOT):
        return True
    cached_cover, _media_type = _cached_embedded_cover(track_id, track_path)
    return bool(cached_cover and cached_cover.is_file())


def _record_local_track_started(track_info: Optional[dict]) -> None:
    if not library_scanner or not track_info:
        return
    if track_info.get("source") != "local":
        return
    track_id = str(track_info.get("id") or "").strip()
    if not track_id:
        return
    try:
        library_scanner.record_track_play(track_id)
    except Exception as exc:
        logger.debug("Failed to update local track play stats for %s: %s", track_id, exc)


@app.get("/api/tracks")
async def list_tracks():
    tracks = library_scanner.get_tracks()
    return [t.to_dict() for t in tracks]


@app.get("/api/tracks/file/{track_id:path}")
async def download_track_file(track_id: str):
    tracks_by_id = {track.id: track for track in library_scanner.get_tracks(refresh=True)}
    track = tracks_by_id.get(track_id)
    if not track or not track.path:
        raise HTTPException(status_code=404, detail="Track not found")
    track_path = track.path.resolve()
    if not _path_within_root(track_path, settings.MUSIC_ROOT):
        raise HTTPException(status_code=403, detail="Track path outside music root")
    if not track_path.is_file():
        raise HTTPException(status_code=404, detail="Track file missing")
    return FileResponse(track_path, filename=track_path.name)


@app.get("/api/tracks/cover/{track_id:path}")
async def get_track_cover(track_id: str):
    tracks_by_id = {track.id: track for track in library_scanner.get_tracks(refresh=False)}
    track = tracks_by_id.get(track_id)
    if not track or not track.path:
        raise HTTPException(status_code=404, detail="Track not found")
    track_path = track.path.resolve()
    if not _path_within_root(track_path, settings.MUSIC_ROOT):
        raise HTTPException(status_code=403, detail="Track path outside music root")
    if not track_path.is_file():
        raise HTTPException(status_code=404, detail="Track file missing")

    folder_cover = _folder_cover_for_track(track_path)
    if folder_cover:
        cover_path = folder_cover.resolve()
        if _path_within_root(cover_path, settings.MUSIC_ROOT):
            return FileResponse(cover_path, media_type=_cover_media_type(cover_path))

    cached_cover, media_type = _cached_embedded_cover(track_id, track_path)
    if cached_cover and cached_cover.is_file():
        return FileResponse(cached_cover, media_type=media_type or _cover_media_type(cached_cover))

    raise HTTPException(status_code=404, detail="Cover not found")


@app.get("/api/tracks/cover-info/{track_id:path}")
async def get_track_cover_info(track_id: str):
    return {"available": _track_cover_available(track_id)}


@app.get("/api/smart/top-tracks")
async def get_smart_top_tracks(limit: int = 40):
    if not library_scanner:
        raise HTTPException(status_code=503, detail="Library not available")
    return library_scanner.get_top_played_tracks(limit=limit)


@app.get("/api/smart/top40/cover")
async def get_smart_top40_cover():
    if not TOP40_COVER_IMAGE.is_file():
        raise HTTPException(status_code=404, detail="Top 40 cover not found")
    return FileResponse(TOP40_COVER_IMAGE, media_type="image/png")


@app.get("/api/albums")
async def list_albums(query: Optional[str] = None):
    """List albums grouped from the local library, optionally filtered by search query."""
    if not library_scanner:
        raise HTTPException(status_code=503, detail="Library not available")
    albums = library_scanner.get_albums()
    if query:
        q = query.strip().lower()
        filtered = []
        for album in albums:
            # Search in album name, artist, genre, year, and track metadata.
            match = (
                q in album["name"].lower()
                or q in album["artist"].lower()
                or q in " ".join(album.get("genres") or []).lower()
                or q in " ".join(str(year) for year in (album.get("years") or [])).lower()
            )
            if not match:
                album_tracks = library_scanner.get_album_tracks(album["id"])
                match = any(
                    q in " ".join(
                        str(value)
                        for value in (
                            t.title,
                            t.artist,
                            t.album,
                            t.album_artist,
                            t.genre,
                            t.year,
                        )
                        if value
                    ).lower()
                    for t in album_tracks
                )
            if match:
                filtered.append(album)
        albums = filtered
    return albums


@app.get("/api/albums/{album_id}/tracks")
async def get_album_tracks(album_id: str):
    """Return tracks for a specific album, sorted by disc/track number."""
    if not library_scanner:
        raise HTTPException(status_code=503, detail="Library not available")
    tracks = library_scanner.get_album_tracks(album_id)
    if not tracks:
        raise HTTPException(status_code=404, detail="Album not found")
    return [t.to_dict() for t in tracks]


@app.post("/api/albums/{album_id}/favorite")
async def set_album_favorite(album_id: str, request: Request):
    """Persist album favorite state in the smart metadata cache."""
    if not library_scanner:
        raise HTTPException(status_code=503, detail="Library not available")
    tracks = library_scanner.get_album_tracks(album_id)
    if not tracks:
        raise HTTPException(status_code=404, detail="Album not found")
    body = await request.json()
    favorite = bool(body.get("favorite"))
    metadata = library_scanner.set_album_favorite(album_id, favorite)
    return {"status": "ok", "album_id": album_id, "favorite": bool(metadata.get("favorite"))}


@app.get("/api/albums/{album_id}/discover")
async def get_album_discover(album_id: str, refresh: bool = False):
    """Return cached similar-music suggestions for an album."""
    if not library_scanner:
        raise HTTPException(status_code=503, detail="Library not available")
    tracks = library_scanner.get_album_tracks(album_id)
    if not tracks:
        raise HTTPException(status_code=404, detail="Album not found")
    result = library_scanner.get_album_discover(album_id, force=refresh)
    return {
        "album_id": album_id,
        "items": result.get("items") or [],
        "source": result.get("source"),
        "cached": bool(result.get("cached")),
        "error": result.get("error"),
    }


@app.get("/api/albums/{album_id}/cover")
async def get_album_cover(album_id: str, size: int = 256):
    """Return cover image for an album, resized to thumbnail.
    Priority: folder cover > embedded cover > external cover > 404.
    """
    if not library_scanner:
        raise HTTPException(status_code=503, detail="Library not available")
    tracks = library_scanner.get_album_tracks(album_id)
    if not tracks:
        raise HTTPException(status_code=404, detail="Album not found")

    # Try folder cover first (from any track in the album)
    for track in tracks:
        if not track.path:
            continue
        folder_cover = _folder_cover_for_track(track.path)
        if folder_cover:
            try:
                return _serve_cover_image(folder_cover, size)
            except Exception as exc:
                logger.warning("Failed to serve folder album cover %s for album %s: %s", folder_cover, album_id, exc)

    # Try embedded cover from first track that has one
    for track in tracks:
        if not track.path:
            continue
        cached_cover, media_type = _cached_embedded_cover(track.id, track.path)
        if cached_cover and cached_cover.is_file():
            try:
                return _serve_cover_image(cached_cover, size)
            except Exception as exc:
                logger.warning("Failed to serve embedded album cover %s for album %s: %s", cached_cover, album_id, exc)

    external_cover = library_scanner.get_album_external_cover(album_id)
    if external_cover and external_cover.is_file():
        try:
            return _serve_cover_image(external_cover, size)
        except Exception as exc:
            logger.warning("Failed to serve external album cover %s for album %s: %s", external_cover, album_id, exc)

    raise HTTPException(status_code=404, detail="Cover not found")


@app.post("/api/tracks/download")
async def download_tracks(req: DownloadTracksRequest):
    global library_scanner, settings
    if not library_scanner or not settings:
        raise HTTPException(status_code=503, detail="Library not available")
    if not req.track_ids:
        raise HTTPException(status_code=400, detail="track_ids is required")

    tracks_by_id = {track.id: track for track in library_scanner.get_tracks(refresh=True)}
    selected_tracks = []
    seen_ids = set()
    for track_id in req.track_ids:
        if not track_id or track_id in seen_ids:
            continue
        seen_ids.add(track_id)
        track = tracks_by_id.get(track_id)
        if not track or not track.path:
            raise HTTPException(status_code=404, detail=f"Track not found: {track_id}")
        track_path = track.path.resolve()
        if not _path_within_root(track_path, settings.MUSIC_ROOT):
            raise HTTPException(status_code=403, detail="Track path outside music root")
        if not track_path.is_file():
            raise HTTPException(status_code=404, detail=f"Track file missing: {track_path.name}")
        selected_tracks.append((track, track_path))

    if not selected_tracks:
        raise HTTPException(status_code=404, detail="No downloadable tracks found")
    if len(selected_tracks) == 1:
        _, track_path = selected_tracks[0]
        return FileResponse(track_path, filename=track_path.name)

    with tempfile.NamedTemporaryFile(prefix="fxroute-library-selection-", suffix=".zip", delete=False) as temp_file:
        temp_zip_path = Path(temp_file.name)

    used_names = set()
    try:
        with zipfile.ZipFile(temp_zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for _, track_path in selected_tracks:
                archive.write(track_path, arcname=_dedupe_archive_name(track_path.name, used_names))
    except Exception:
        temp_zip_path.unlink(missing_ok=True)
        raise

    return FileResponse(
        temp_zip_path,
        filename="fxroute-library-selection.zip",
        media_type="application/zip",
        background=BackgroundTask(_cleanup_temp_file, temp_zip_path),
    )


@app.get("/api/playlists")
async def list_playlists():
    return [
        {
            "id": playlist.id,
            "name": playlist.name,
            "track_ids": playlist.track_ids,
            "track_count": len(playlist.track_ids),
        }
        for playlist in get_playlists()
    ]


@app.post("/api/playlists")
async def create_or_update_playlist(req: PlaylistSaveRequest):
    try:
        playlist = save_playlist(req.name, req.track_ids)
        return {
            "status": "ok",
            "playlist": {
                "id": playlist.id,
                "name": playlist.name,
                "track_ids": playlist.track_ids,
                "track_count": len(playlist.track_ids),
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/playlists/{playlist_id}/export")
async def export_playlist(playlist_id: str):
    if not library_scanner or not settings:
        raise HTTPException(status_code=503, detail="Library not available")
    playlist = next((item for item in get_playlists() if item.id == playlist_id), None)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    content = _build_m3u_for_playlist(playlist)
    filename = _playlist_download_filename(playlist.name)
    return Response(
        content=content,
        media_type="audio/x-mpegurl; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/playlists/{playlist_id}")
async def remove_playlist(playlist_id: str):
    try:
        delete_playlist(playlist_id)
        return {"status": "ok", "deleted": playlist_id}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/library/upload")
async def upload_track(file: UploadFile = File(...)):
    global library_scanner, settings
    if not library_scanner or not settings:
        raise HTTPException(status_code=503, detail="Library not available")

    filename = Path(file.filename or "").name.strip()
    if not filename:
        raise HTTPException(status_code=400, detail="A filename is required")

    suffix = Path(filename).suffix.lower()
    if suffix not in UPLOAD_AUDIO_EXTENSIONS and suffix not in PLAYLIST_FILE_EXTENSIONS and suffix != ".zip":
        raise HTTPException(status_code=400, detail="Unsupported file type")

    target_dir = settings.download_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = None
    album_dir = None
    temp_zip_path = None

    try:
        if suffix == ".zip":
            temp_zip_path = _choose_unique_path(target_dir / filename)
            with temp_zip_path.open("wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            album_dir = _choose_unique_dir(target_dir / Path(filename).stem)
            album_dir.mkdir(parents=True, exist_ok=False)

            try:
                extraction = _extract_zip_album(temp_zip_path, album_dir)
                audio_files = extraction["audio_files"]
                playlist_files = extraction["playlist_files"]
                if not audio_files and not playlist_files:
                    shutil.rmtree(album_dir, ignore_errors=True)
                    raise HTTPException(status_code=400, detail="ZIP contains no supported audio or playlist files")
            except Exception:
                shutil.rmtree(album_dir, ignore_errors=True)
                raise
            finally:
                temp_zip_path.unlink(missing_ok=True)

            tracks = library_scanner.refresh(force=True)
            imported_playlists = []
            for playlist_path in playlist_files:
                imported = _import_m3u_playlist(
                    playlist_path.name,
                    playlist_path.read_text(encoding="utf-8", errors="replace"),
                    base_dir=playlist_path.parent,
                    tracks=tracks,
                )
                if imported:
                    imported_playlists.append(imported)
            if not audio_files and not imported_playlists:
                shutil.rmtree(album_dir, ignore_errors=True)
                raise HTTPException(status_code=400, detail="Playlist did not match any library tracks")
            playlist_part = f" and {len(imported_playlists)} playlist{'s' if len(imported_playlists) != 1 else ''}" if imported_playlists else ""
            return {
                "status": "imported",
                "kind": "zip",
                "filename": filename,
                "folder": album_dir.name,
                "path": str(album_dir),
                "track_count": len(tracks),
                "imported_track_count": len(audio_files),
                "imported_playlist_count": len(imported_playlists),
                "playlists": imported_playlists,
                "skipped_entry_count": len(extraction["skipped_entries"]),
                "message": f"Imported {len(audio_files)} track{'s' if len(audio_files) != 1 else ''}{playlist_part} from {filename}",
            }

        if suffix in PLAYLIST_FILE_EXTENSIONS:
            content = (await file.read()).decode("utf-8", errors="replace")
            tracks = library_scanner.get_tracks(refresh=True)
            imported = _import_m3u_playlist(filename, content, tracks=tracks)
            if not imported:
                raise HTTPException(status_code=400, detail="Playlist did not match any library tracks")
            return {
                "status": "imported",
                "kind": "playlist",
                "filename": filename,
                "track_count": len(tracks),
                "imported_playlist_count": 1,
                "playlist": imported,
                "message": f"Imported playlist {imported['name']} with {imported['track_count']} track{'s' if imported['track_count'] != 1 else ''}",
            }

        target_path = target_dir / filename
        if target_path.exists():
            raise HTTPException(status_code=409, detail="A file with that name already exists")

        with target_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        tracks = library_scanner.refresh(force=True)
        return {
            "status": "uploaded",
            "kind": "audio",
            "filename": filename,
            "path": str(target_path),
            "track_count": len(tracks),
            "message": f"Uploaded {filename}",
        }
    except HTTPException:
        if temp_zip_path and temp_zip_path.exists():
            temp_zip_path.unlink(missing_ok=True)
        if album_dir and album_dir.exists() and not any(album_dir.iterdir()):
            album_dir.rmdir()
        raise
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        if temp_zip_path and temp_zip_path.exists():
            temp_zip_path.unlink(missing_ok=True)
        if target_path and target_path.exists():
            target_path.unlink(missing_ok=True)
        if album_dir and album_dir.exists():
            shutil.rmtree(album_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Upload failed")
    finally:
        await file.close()


@app.post("/api/tracks/delete")
async def delete_tracks(req: DeleteTracksRequest):
    global library_scanner, settings
    if not library_scanner or not settings:
        raise HTTPException(status_code=503, detail="Library not available")

    if not req.track_ids:
        raise HTTPException(status_code=400, detail="track_ids is required")

    tracks_by_id = {track.id: track for track in library_scanner.get_tracks(refresh=True)}
    deleted = []
    errors = []
    affected_folders = set()
    music_root = settings.MUSIC_ROOT.resolve()

    for track_id in req.track_ids:
        track = tracks_by_id.get(track_id)
        if not track or not track.path:
            errors.append({"track_id": track_id, "error": "Track not found"})
            continue

        try:
            path = track.path.resolve()
            if not _path_within_root(path, music_root) or not path.is_file():
                errors.append({"track_id": track_id, "error": "Track path outside music root"})
                continue
            parent = path.parent
            path.unlink()
            deleted.append(track_id)
            affected_folders.add(parent)
        except Exception as e:
            errors.append({"track_id": track_id, "error": str(e)})

    cleanup = [
        _cleanup_track_parent_folder(folder, music_root, {settings.download_dir.resolve()})
        for folder in sorted(affected_folders)
    ]
    tracks = library_scanner.refresh(force=True)
    return {
        "status": "ok",
        "deleted": deleted,
        "errors": errors,
        "cleanup": cleanup,
        "track_count": len(tracks),
    }


@app.post("/api/library/folders/delete")
async def delete_library_folder(req: DeleteFolderRequest):
    global library_scanner, settings
    if not library_scanner or not settings:
        raise HTTPException(status_code=503, detail="Library not available")

    music_root = settings.MUSIC_ROOT.resolve()
    folder_path = _resolve_library_folder(req.folder, music_root)
    if folder_path == settings.download_dir.resolve():
        raise HTTPException(status_code=400, detail="Cannot delete the managed imports container")
    rel_folder = folder_path.relative_to(music_root).as_posix()

    deleted_track_ids = []
    for track in library_scanner.get_tracks(refresh=True):
        if not track.path:
            continue
        try:
            if track.path.resolve().is_relative_to(folder_path):
                deleted_track_ids.append(track.id)
        except (OSError, ValueError):
            continue

    try:
        shutil.rmtree(folder_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete folder: {exc}") from exc

    tracks = library_scanner.refresh(force=True)
    return {
        "status": "ok",
        "folder": rel_folder,
        "deleted": deleted_track_ids,
        "folder_removed": not folder_path.exists(),
        "track_count": len(tracks),
    }


@app.post("/api/play")
async def play_track(req: PlayRequest):
    global current_track_info, last_track_info, last_radio_track_info, playback_queue_mode
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if not _can_send_play_command():
        state = player_instance.state
        return {
            "status": "playing" if not state.get("paused") else "paused",
            "url": state.get("current_file") or "",
            "track": current_track_info or last_track_info or {},
            "playback": build_playback_payload(state),
        }

    source = str(req.source or "local")
    if source not in {"local", "radio"}:
        raise HTTPException(status_code=400, detail=f"Unsupported playback source: {source}")

    previous_track = dict(current_track_info or {})
    previous_state = dict(player_instance.state)
    if source == "radio":
        _clear_playback_queue()
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
    else:
        saved_context = current_track_info
        saved_last_context = last_track_info
        try:
            active_queue_ids = [item.get("id") for item in playback_queue]
            _clear_playback_queue()
            preserve_queue_order = bool(req.queue_track_ids) and list(req.queue_track_ids) == active_queue_ids
            track_info = _prepare_local_queue(
                req.track_id,
                req.queue_track_ids,
                shuffle=req.shuffle,
                loop=req.loop,
                reshuffle=not preserve_queue_order,
            )
        finally:
            # Queue preparation is metadata-only.  The active track context is
            # committed only after the coordinator's readback gate succeeds.
            current_track_info = saved_context
            last_track_info = saved_last_context
    if not track_info or not track_info.get("url"):
        raise HTTPException(status_code=404, detail="Track not found")

    target_url = str(track_info.get("url") or "")
    native_queue_fields = _native_queue_request_fields() if source == "local" else {}
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
        if native_queue_fields:
            playback_queue_mode = "app_replace"
        raise _transition_error_http(exc) from exc
    if source in {"local", "radio"} and isinstance(result.target_rate, int) and result.target_rate > 0:
        track_info["sample_rate_hz"] = result.target_rate

    _commit_coordinated_track(track_info, source=source)
    if native_queue_fields:
        playback_queue_mode = "native_mpv"
    return {
        "status": "playing",
        "url": target_url,
        "track": track_info,
        "playback": build_playback_payload(player_instance.state),
    }

@app.post("/api/pause")
async def pause_playback():
    global player_instance
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")

    state = player_instance.state
    if not state.get("current_file") or state.get("ended"):
        raise HTTPException(status_code=409, detail="Nothing is currently loaded to pause or resume")
    # v0.9.4 contract: this endpoint is a pure MPV pause toggle.  It must not
    # rebuild the committed source/rate/graph just because transport changed.
    player_instance.pause()
    new_state = player_instance.state
    _mark_player_state_authoritative(new_state)
    _mark_playback_intent_changed()
    return {
        "status": "paused" if new_state.get("paused") else "playing",
        "playback": build_playback_payload(new_state),
    }


@app.post("/api/playback/toggle")
async def toggle_playback():
    global current_track_info, last_track_info, last_radio_track_info
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if not _can_send_play_command():
        state = player_instance.state
        return {"status": "paused" if state.get("paused") else "playing", "playback": build_playback_payload(state)}

    state = dict(player_instance.state)
    active_track = dict(current_track_info or {})
    if state.get("current_file") and not state.get("ended") and active_track.get("source") in {"local", "radio"}:
        was_paused = bool(state.get("paused"))
        source = str(active_track.get("source"))
        if not was_paused:
            # Same-source pause is transport only.  Resuming below remains a
            # Coordinator transition because it is a Local/Radio play action.
            player_instance.pause()
            new_state = player_instance.state
            _mark_player_state_authoritative(new_state)
            _mark_playback_intent_changed()
            return {
                "status": "playing" if not new_state.get("paused") else "paused",
                "playback": build_playback_payload(new_state),
            }
        target_rate = _coordinator_target_rate(source, active_track)
        request = TransitionRequest(
            operation="resume",
            source=source,
            target_rate=target_rate,
            target_url=str(active_track.get("url") or state.get("current_file") or ""),
            target_track=active_track,
            should_play=True,
            rate_change=_coordinator_rate_change(target_rate),
            reload_source=(target_rate is None or _coordinator_rate_change(target_rate)),
            detail="toggle-resume",
        )
        try:
            result = await _run_coordinated_transition(request)
        except PlaybackTransitionFailure as exc:
            raise _transition_error_http(exc) from exc
        if was_paused:
            if source in {"local", "radio"} and isinstance(result.target_rate, int) and result.target_rate > 0:
                active_track["sample_rate_hz"] = result.target_rate
            _commit_coordinated_track(active_track, source=source)
        new_state = player_instance.state
        return {
            "status": "playing" if not new_state.get("paused") else "paused",
            "playback": build_playback_payload(new_state),
        }

    replay_track = dict(current_track_info or last_radio_track_info or {})
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
    )
    try:
        result = await _run_coordinated_transition(request)
    except PlaybackTransitionFailure as exc:
        raise _transition_error_http(exc) from exc
    if source in {"local", "radio"} and isinstance(result.target_rate, int) and result.target_rate > 0:
        replay_track["sample_rate_hz"] = result.target_rate
    _commit_coordinated_track(replay_track, source=source)
    return {
        "status": "playing",
        "replayed": True,
        "playback": build_playback_payload(player_instance.state),
    }

@app.post("/api/stop")
async def stop_playback():
    global player_instance, current_track_info, last_radio_track_info, radio_reconnect_attempts, radio_reconnect_url, radio_reconnect_active_since
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if current_track_info and current_track_info.get("source") == "radio":
        last_radio_track_info = dict(current_track_info)
    _mark_playback_intent_changed()
    current_track_info = None
    radio_reconnect_attempts = 0
    radio_reconnect_url = None
    radio_reconnect_active_since = 0.0
    _clear_playback_queue()
    _reset_mpv_loop_state()
    player_instance.stop_playback()
    _mark_player_state_authoritative(player_instance.state)
    return {"status": "stopped"}

@app.post("/api/volume")
async def set_volume(request: Request):
    global player_instance
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    try:
        body = await request.json()
        vol = int(body.get("volume", 50))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body, expected {\"volume\": <int>}")
    try:
        volume_result = _set_canonical_output_volume(vol)
    except SystemVolumeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to set output volume: {exc}")
    ensure_local_source_volume()
    # Keep local/radio output-volume changes responsive. Spotify volume uses
    # /api/spotify/volume, so this endpoint should not block on multiple
    # playerctl/Spotify status reads on slow boards.
    await manager.broadcast({"type": "playback", "data": build_playback_payload(player_instance.state)})
    return {
        "volume": volume_result["volume"],
        **({"loudnessVolumeDb": volume_result["loudnessVolumeDb"]}
           if volume_result.get("loudness_enabled") else {}),
    }

@app.post("/api/playback/next")
async def next_playback():
    global player_instance
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if len(playback_queue) <= 1:
        raise HTTPException(status_code=409, detail="No queue is active")
    if not await _advance_playback_queue(transition_reason="manual queue next"):
        raise HTTPException(status_code=409, detail="Already at the end of the queue")
    return {"status": "playing", "playback": build_playback_payload(player_instance.state)}


@app.post("/api/playback/previous")
async def previous_playback():
    global player_instance
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if len(playback_queue) <= 1:
        raise HTTPException(status_code=409, detail="No queue is active")
    if not await _rewind_playback_queue(transition_reason="manual queue previous"):
        raise HTTPException(status_code=409, detail="Already at the start of the queue")
    return {"status": "playing", "playback": build_playback_payload(player_instance.state)}


@app.post("/api/playback/clear-queue")
async def clear_playback_queue():
    global player_instance
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")

    had_queue = len(playback_queue) > 1
    _clear_playback_queue()
    playback = build_playback_payload(player_instance.state)
    await manager.broadcast({"type": "playback", "data": playback})
    return {"status": "cleared" if had_queue else "idle", "playback": playback}


@app.post("/api/playback/selection")
async def sync_playback_selection(request: Request):
    global player_instance
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    queue_track_ids = body.get("queue_track_ids") or []
    if not isinstance(queue_track_ids, list):
        raise HTTPException(status_code=400, detail="Invalid JSON, expected {\"queue_track_ids\": <list>}")

    playback = _sync_active_local_queue_selection(
        queue_track_ids=queue_track_ids,
        shuffle=bool(body.get("shuffle", False)),
        loop=bool(body.get("loop", False)),
    )
    await manager.broadcast({"type": "playback", "data": playback})
    return {"status": "ok", "playback": playback}


@app.post("/api/playback/shuffle")
async def set_playback_shuffle(request: Request):
    global player_instance
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    try:
        body = await request.json()
        enabled = bool(body.get("enabled", False))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON, expected {\"enabled\": <bool>}")

    try:
        if not await _set_queue_shuffle(enabled):
            raise HTTPException(status_code=409, detail="Shuffle requires an active local queue")
    except PlaybackTransitionFailure as exc:
        raise _transition_error_http(exc) from exc

    playback = build_playback_payload(player_instance.state)
    await manager.broadcast({"type": "playback", "data": playback})
    return {"status": "ok", "shuffle": playback["queue"].get("shuffle", False), "playback": playback}


@app.post("/api/playback/loop")
async def set_playback_loop(request: Request):
    global player_instance
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    try:
        body = await request.json()
        enabled = bool(body.get("enabled", False))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON, expected {\"enabled\": <bool>}")

    if not _set_queue_loop(enabled):
        raise HTTPException(status_code=409, detail="Loop requires active local playback")

    playback = build_playback_payload(player_instance.state)
    await manager.broadcast({"type": "playback", "data": playback})
    return {"status": "ok", "loop": playback["queue"].get("loop", False), "playback": playback}


@app.post("/api/playback/seek")
async def seek_playback(request: Request):
    global player_instance
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if not _can_send_play_command():
        state = player_instance.state
        return {"status": "ok", "position": state.get("position", 0), "playback": build_playback_payload(state)}
    try:
        body = await request.json()
        pos = float(body.get("position", 0))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON, expected {\"position\": <float>}")
    if not player_instance.state.get("current_file"):
        raise HTTPException(status_code=409, detail="Nothing loaded to seek")
    player_instance.seek(pos)
    _mark_playback_intent_changed()
    return {"status": "ok", "position": pos, "playback": build_playback_payload(player_instance.state)}

@app.get("/api/status")
async def get_status():
    if player_instance:
        state = build_playback_payload(player_instance.state)
        state["metadata"] = player_instance.get_metadata() if state.get("current_file") else {}
        track = state.get("current_track") or {}
        if track.get("source") == "radio":
            station_id = str(track.get("id") or "").removeprefix("radio_")
            stream_url = str(track.get("url") or "")
            # Provider lookup is metadata-only and occurs after the playback
            # payload has been built.  It cannot affect loadfile or audio state.
            provider_metadata = await radio_metadata_service.get(station_id, stream_url)
            active_track = (current_track_info or {})
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
                player_instance.get_stream_audio_info()
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


@app.get("/api/system/update")
async def system_update_status():
    result = await _run_update_script("--check")
    return {
        "ok": result["returncode"] == 0,
        "installed_version": _read_version_file(),
        **result,
    }


@app.post("/api/system/update")
async def system_update():
    service_name = _configured_service_name()
    result = await _run_update_script("--defer-restart")
    ok = result["returncode"] == 0
    update_applied = ok and "Pulling updates with fast-forward only." in result.get("stdout", "")
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
    result = await _run_update_script("--restore", "--defer-restart")
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
    status = get_samplerate_status()
    logger.info(
        "audio_samplerate_status entry: footer_owner=%s active_rate=%s sink_state=%s",
        current_footer_owner,
        status.get("active_rate"),
        (status.get("relevant_sink") or {}).get("state"),
    )
    # This endpoint is a pure readback.  Any corrective action must enter the
    # PlaybackTransitionCoordinator through an explicit recovery request.
    return status


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
    overview = _with_subwoofer_derived_delays(get_audio_output_overview())
    if subwoofer_runtime is not None:
        overview["output_mode"] = {
            **(overview.get("output_mode") or {}),
            "runtime": subwoofer_runtime.snapshot(),
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
        await _sync_subwoofer_runtime(result, reason="output-selection")
        result = _with_subwoofer_derived_delays(result)
        if subwoofer_runtime is not None:
            result["output_mode"] = {
                **(result.get("output_mode") or {}),
                "runtime": subwoofer_runtime.snapshot(),
            }
        await refresh_peak_monitor_after_effects_change("audio-output-switch")
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

    if measurement_sr_session is not None and measurement_sr_session.active:
        raise HTTPException(status_code=423, detail="Measurement is active; output mode switch is locked")

    try:
        target = prepare_audio_output_mode(mode, subwoofer, subwoofers)
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

        result = _with_subwoofer_derived_delays(get_audio_output_overview())
        if subwoofer_runtime is not None:
            result["output_mode"] = {
                **(result.get("output_mode") or {}),
                "runtime": subwoofer_runtime.snapshot(),
            }
        await refresh_peak_monitor_after_effects_change("audio-output-mode-switch")
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
    global player_instance
    try:
        if player_instance and player_instance._running:
            player_instance.stop_playback()
            await manager.broadcast({"type": "playback", "data": build_playback_payload(player_instance.state)})
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

    global current_source_mode
    try:
        result = set_audio_source_selection(mode, input_key)
        result = await _sync_external_input_monitoring(result)
        result = await _sync_bluetooth_input_monitoring(result)
        current_source_mode = result.get("mode") or SOURCE_MODE_APP_PLAYBACK
        if result.get("mode") in {SOURCE_MODE_EXTERNAL_INPUT, SOURCE_MODE_BLUETOOTH_INPUT}:
            await _pause_all_app_playback_for_external_input()
        await sync_peak_monitor_for_source_mode_state(result)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save source mode: {exc}")


def _parse_effects_extras_from_json(body: dict) -> dict:
    """Thin wrapper: EasyEffects extras parsing lives in effects_extras (REFACTOR-010)."""
    return effects_extras.parse_effects_extras_from_json(body)


def _merge_effects_extras_from_json(previous: dict, body: dict) -> dict:
    """Thin wrapper: EasyEffects extras merge lives in effects_extras (REFACTOR-010)."""
    return effects_extras.merge_effects_extras_from_json(previous, body)


def _resolve_effects_extras(extras: dict | None = None) -> dict:
    global easyeffects_manager
    if not easyeffects_manager:
        return extras or {}
    if extras is None:
        return easyeffects_manager.load_global_extras()
    return easyeffects_manager.normalize_effects_extras(extras)


def _is_pure_loudness_strength_change(previous: dict, current: dict) -> bool:
    """Thin wrapper: strength-change detection lives in effects_extras (REFACTOR-010)."""
    return effects_extras.is_pure_loudness_strength_change(previous, current)


def _is_runtime_autogain_loudness_change(previous: dict, current: dict) -> bool:
    """Thin wrapper: autogain/loudness change detection lives in effects_extras (REFACTOR-010)."""
    return effects_extras.is_runtime_autogain_loudness_change(previous, current)


def _require_easyeffects_manager():
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")
    return easyeffects_manager


def _effects_extras_from_form(
    *,
    limiter_enabled: bool,
    headroom_enabled: bool,
    headroom_gain_db: float,
    autogain_enabled: bool,
    autogain_target_db: float,
    delay_enabled: bool,
    delay_left_ms: float,
    delay_right_ms: float,
    tone_effect_enabled: bool,
    tone_effect_mode: str,
    bass_enabled: bool | None = None,
    bass_amount: float | None = None,
) -> dict:
    extras = {
        "limiter": {"enabled": limiter_enabled},
        "headroom": {"enabled": headroom_enabled, "params": {"gainDb": headroom_gain_db}},
        "autogain": {"enabled": autogain_enabled, "params": {"targetDb": autogain_target_db}},
        "delay": {
            "enabled": delay_enabled,
            "params": {"leftMs": delay_left_ms, "rightMs": delay_right_ms},
        },
        "tone_effect": {"enabled": tone_effect_enabled, "mode": tone_effect_mode},
    }
    if bass_enabled is not None or bass_amount is not None:
        extras["bass_enhancer"] = {
            "enabled": bool(bass_enabled),
            "params": {"amount": 0.0 if bass_amount is None else bass_amount},
        }
    if easyeffects_manager:
        extras["loudness"] = easyeffects_manager.load_global_extras().get("loudness", {})
    return _resolve_effects_extras(extras)


async def _finish_easyeffects_preset_mutation(
    *,
    load_after_create: bool,
    preset_name: str,
    refresh_reason: str,
    refresh_only_when_loaded: bool = False,
) -> dict:
    ee_manager = _require_easyeffects_manager()
    if load_after_create:
        ee_manager.load_preset(preset_name)
    status = ee_manager.get_status()
    await manager.broadcast({"type": "easyeffects", "data": status})
    if load_after_create or not refresh_only_when_loaded:
        schedule_peak_monitor_refresh_after_effects_change(refresh_reason)
    return status


def _raise_easyeffects_http_error(exc: Exception) -> None:
    if isinstance(exc, FileNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, RuntimeError):
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    raise exc

@app.get("/api/easyeffects/extras")
async def get_easyeffects_extras():
    ee_manager = _require_easyeffects_manager()
    return {
        "status": "ok",
        "extras": ee_manager.load_global_extras(),
        "excluded_presets": sorted(ee_manager.EXCLUDED_GLOBAL_EXTRAS_PRESETS),
    }

@app.post("/api/easyeffects/extras")
async def save_easyeffects_extras(request: Request):
    ee_manager = _require_easyeffects_manager()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    previous = ee_manager.load_global_extras()
    parsed = _merge_effects_extras_from_json(previous, body)
    was_loudness = bool(previous.get("loudness", {}).get("enabled"))
    enabling_loudness = bool(parsed.get("loudness", {}).get("enabled")) and not was_loudness
    disabling_loudness = was_loudness and not bool(parsed.get("loudness", {}).get("enabled"))
    if enabling_loudness:
        raw_volume = get_output_volume()
        parsed["loudness"]["params"]["volumeDb"] = ee_manager.loudness_db_from_percent(raw_volume)
    try:
        extras = _resolve_effects_extras(parsed)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_effects_extras", "message": str(exc)},
        ) from exc
    if extras == previous:
        logger.info("Ignored unchanged EasyEffects extras update")
        return {
            "status": "ok",
            "extras": extras,
            "updated_presets": 0,
            "skipped_presets": [],
        }
    runtime_strength_change = _is_pure_loudness_strength_change(previous, extras)
    runtime_autogain_loudness_change = _is_runtime_autogain_loudness_change(
        previous, extras
    )
    disabling_master_percent = None
    if disabling_loudness:
        volume_db = float(extras["loudness"]["params"]["volumeDb"])
        disabling_master_percent = ee_manager.loudness_percent_from_db(volume_db)
        # Move the canonical attenuation back to the system master while the
        # Loudness block is still active and guarded.  Bypassing first leaves
        # a short 100%-master window and produces a positive transient.
        set_output_volume(disabling_master_percent)
    try:
        if runtime_strength_change:
            result = ee_manager.apply_loudness_strength_runtime(previous, extras)
        elif runtime_autogain_loudness_change:
            result = ee_manager.apply_autogain_loudness_runtime(previous, extras)
        else:
            result = ee_manager.apply_global_extras_to_all_presets(extras)
    except Exception:
        if disabling_master_percent is not None:
            try:
                set_output_volume(100)
            except Exception:
                logger.exception(
                    "Failed to restore system master after Loudness disable failure"
                )
        raise

    active_preset = ee_manager.get_active_preset()
    if (
        not runtime_autogain_loudness_change
        and active_preset
        and active_preset not in ee_manager.EXCLUDED_GLOBAL_EXTRAS_PRESETS
    ):
        try:
            ee_manager.load_preset(active_preset)
        except Exception as e:
            logger.warning("Failed to reload active preset after extras update: %s", e)
    if enabling_loudness:
        set_output_volume(100)

    status = ee_manager.get_status()
    await manager.broadcast({"type": "easyeffects", "data": status})
    if not runtime_autogain_loudness_change:
        schedule_peak_monitor_refresh_after_effects_change("global-extras-update")
    return {
        "status": "ok",
        "extras": result["extras"],
        "updated_presets": result["updated"],
        "skipped_presets": result["skipped"],
    }


def _spl_output_profile() -> dict[str, str]:
    overview = get_audio_output_overview()
    selected = overview.get("selected_output") if isinstance(overview, dict) else {}
    selected = selected if isinstance(selected, dict) else {}
    key = str(selected.get("key") or selected.get("target") or "default")
    label = str(selected.get("target_label") or selected.get("label") or key)
    return {"id": key, "label": label}


class _Umik1Profile:
    model = "UMIK-1"
    vendor_id = "2752"
    product_id = "0007"
    internal_gain_db = 18
    reference_capture_gain_db = 0.0
    reference_capture_volume_percent = 100.0

    @staticmethod
    def parse_calibration_header(path: Path) -> dict[str, Any]:
        try:
            first_lines = "\n".join(path.read_text(encoding="utf-8", errors="ignore").splitlines()[:8])
        except Exception:
            return {}
        sensitivity = re.search(
            r"Sens\s*Factor\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*dB",
            first_lines,
            flags=re.IGNORECASE,
        )
        serial = re.search(r"SERNO\s*:\s*([0-9][0-9 -]*)", first_lines, flags=re.IGNORECASE)
        return {
            "sensitivity_factor_db": float(sensitivity.group(1)) if sensitivity else None,
            "serial_number": re.sub(r"\D", "", serial.group(1)) if serial else "",
        }

    @classmethod
    def calibration_reference(cls, path: Path, filename: str) -> dict[str, Any]:
        header = cls.parse_calibration_header(path)
        sensitivity = header.get("sensitivity_factor_db")
        serial_number = str(header.get("serial_number") or "")
        filename_serial = "".join(re.findall(r"\d", Path(filename).stem))
        return {
            "sensitivity_factor_db": sensitivity,
            "serial_number": serial_number,
            "serial_matches_filename": bool(
                serial_number and filename_serial and serial_number == filename_serial
            ),
        }

    @classmethod
    def matches_input(cls, item: dict[str, Any]) -> bool:
        vendor = str(item.get("device_vendor_id") or "").lower().replace("0x", "").zfill(4)
        product = str(item.get("device_product_id") or "").lower().replace("0x", "").zfill(4)
        metadata = " ".join(
            str(item.get(key) or "")
            for key in (
                "node_name", "node_description", "device_name", "device_description",
                "alsa_card_name", "alsa_long_card_name",
            )
        ).lower()
        return vendor == cls.vendor_id and product == cls.product_id and "umik-1" in metadata

    @classmethod
    def has_reference_internal_gain(cls, item: dict[str, Any]) -> bool:
        metadata = " ".join(
            str(item.get(key) or "")
            for key in ("node_description", "device_description", "alsa_card_name", "alsa_long_card_name")
        )
        return bool(
            re.search(
                rf"gain\s*[: ]+\s*{cls.internal_gain_db}\s*dB",
                metadata,
                flags=re.IGNORECASE,
            )
        )


_UMIK1_PROFILE = _Umik1Profile()


class _Umik2Profile:
    model = "UMIK-2"
    vendor_id = "2752"
    product_id = "002b"
    factory_analog_gain_db = 18.0
    reference_capture_gain_db = 0.0
    reference_capture_volume_percent = 100.0

    @staticmethod
    def parse_calibration_header(path: Path) -> dict[str, Any]:
        try:
            first_lines = "\n".join(path.read_text(encoding="utf-8", errors="ignore").splitlines()[:8])
        except Exception:
            return {}
        sensitivity = re.search(
            r"Sens\s*Factor\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*dB",
            first_lines,
            flags=re.IGNORECASE,
        )
        analog_gain = re.search(
            r"AGain\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*dB",
            first_lines,
            flags=re.IGNORECASE,
        )
        serial = re.search(r"SERNO\s*:\s*([0-9][0-9 -]*)", first_lines, flags=re.IGNORECASE)
        return {
            "sensitivity_factor_db": float(sensitivity.group(1)) if sensitivity else None,
            "analog_gain_db": float(analog_gain.group(1)) if analog_gain else None,
            "serial_number": re.sub(r"\D", "", serial.group(1)) if serial else "",
        }

    @classmethod
    def calibration_reference(cls, path: Path, filename: str) -> dict[str, Any]:
        header = cls.parse_calibration_header(path)
        serial_number = str(header.get("serial_number") or "")
        filename_serial = "".join(re.findall(r"\d", Path(filename).stem))
        return {
            **header,
            "serial_matches_filename": bool(
                serial_number and filename_serial and serial_number == filename_serial
            ),
        }

    @classmethod
    def matches_input(cls, item: dict[str, Any]) -> bool:
        vendor = str(item.get("device_vendor_id") or "").lower().replace("0x", "").zfill(4)
        product = str(item.get("device_product_id") or "").lower().replace("0x", "").zfill(4)
        metadata = " ".join(
            str(item.get(key) or "")
            for key in (
                "node_name", "node_description", "device_name", "device_description",
                "alsa_card_name", "alsa_long_card_name",
            )
        ).lower()
        return vendor == cls.vendor_id and product == cls.product_id and "umik-2" in metadata


_UMIK2_PROFILE = _Umik2Profile()


class _Umm6Profile:
    model = "Dayton UMM-6"
    vendor_id = "0d8c"
    product_id = "0147"
    reference_sensitivity_dbfs_per_pa = -19.0
    reference_ipga_db = 30.0
    reference_capture_gain_db = 0.0
    reference_capture_volume_percent = 100.0

    @staticmethod
    def parse_calibration_header(path: Path) -> dict[str, Any]:
        try:
            first_lines = "\n".join(path.read_text(encoding="utf-8", errors="ignore").splitlines()[:8])
        except Exception:
            return {}
        sensitivity = re.search(
            r"Sens\s*Factor\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*dB",
            first_lines,
            flags=re.IGNORECASE,
        )
        serial = re.search(r"SERNO\s*:\s*([0-9][0-9 -]*)", first_lines, flags=re.IGNORECASE)
        return {
            "sensitivity_factor_db": float(sensitivity.group(1)) if sensitivity else None,
            "serial_number": re.sub(r"\D", "", serial.group(1)) if serial else "",
        }

    @classmethod
    def calibration_reference(cls, path: Path, filename: str) -> dict[str, Any]:
        header = cls.parse_calibration_header(path)
        serial_number = str(header.get("serial_number") or "")
        filename_serial = "".join(re.findall(r"\d", Path(filename).stem))
        return {
            **header,
            "serial_matches_filename": bool(
                serial_number and filename_serial and serial_number == filename_serial
            ),
        }

    @classmethod
    def matches_input(cls, item: dict[str, Any]) -> bool:
        vendor = str(item.get("device_vendor_id") or "").lower().replace("0x", "").zfill(4)
        product = str(item.get("device_product_id") or "").lower().replace("0x", "").zfill(4)
        metadata = " ".join(
            str(item.get(key) or "")
            for key in (
                "node_name", "node_description", "device_name", "device_description",
                "device_product_name", "alsa_card_name", "alsa_long_card_name",
            )
        ).lower()
        model_match = bool(re.search(r"\bumm[\s_-]*6\b", metadata))
        return vendor == cls.vendor_id and product == cls.product_id and model_match

    @staticmethod
    def parse_calibration_curve(path: Path) -> tuple[Any, Any, Any] | None:
        import numpy as calibration_np

        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return None
        points: list[tuple[float, float, float]] = []
        for raw_line in lines:
            parts = re.split(r"[\s,;]+", raw_line.strip())
            if len(parts) != 3:
                continue
            try:
                frequency, correction, phase = (float(value) for value in parts)
            except ValueError:
                continue
            if (
                frequency > 0.0
                and math.isfinite(frequency)
                and math.isfinite(correction)
                and math.isfinite(phase)
            ):
                points.append((frequency, correction, phase))
        if len(points) < 2:
            return None
        points.sort(key=lambda point: point[0])
        if any(points[index][0] == points[index - 1][0] for index in range(1, len(points))):
            return None
        return tuple(
            calibration_np.asarray(values, dtype=calibration_np.float64)
            for values in zip(*points)
        )


_UMM6_PROFILE = _Umm6Profile()


def _parse_umik_calibration_header(path: Path) -> dict[str, Any]:
    return _UMIK1_PROFILE.parse_calibration_header(path)


def _is_umik1_input(item: dict[str, Any]) -> bool:
    return _UMIK1_PROFILE.matches_input(item)


def _is_umik2_input(item: dict[str, Any]) -> bool:
    return _UMIK2_PROFILE.matches_input(item)


def _is_umm6_input(item: dict[str, Any]) -> bool:
    return _UMM6_PROFILE.matches_input(item)


def _spl_auto_capability() -> dict[str, Any]:
    calibration_state = measurement_store.get_calibration_state() if measurement_store else {}
    active_id = str(calibration_state.get("active_calibration_file_id") or "")
    entries = calibration_state.get("calibrations") if isinstance(calibration_state, dict) else []
    active = next((item for item in (entries or []) if str(item.get("id") or "") == active_id), {})
    name = str(active.get("filename") or "")
    path = Path(str(active.get("path") or "")) if active else Path()
    settings = _read_measurement_setup_settings()
    selected_id = str(settings.get("selectedInputId") or "")
    inputs_payload = measurement_store.list_inputs() if measurement_store else {}
    inputs = inputs_payload.get("inputs") if isinstance(inputs_payload, dict) else []
    selected = next((item for item in (inputs or []) if str(item.get("id") or "") == selected_id), {})
    profile = (
        _UMIK1_PROFILE if selected and _is_umik1_input(selected)
        else _UMIK2_PROFILE if selected and _is_umik2_input(selected)
        else _UMM6_PROFILE if selected and _is_umm6_input(selected)
        else None
    )
    calibration = (
        profile.calibration_reference(path, name)
        if profile and active and path.is_file()
        else {}
    )
    calibration_curve_valid = (
        _UMM6_PROFILE.parse_calibration_curve(path) is not None
        if profile is _UMM6_PROFILE and active and path.is_file()
        else True
    )
    umik_inputs = [item for item in (inputs or []) if profile and profile.matches_input(item)]

    supported_model = profile.model if profile else None
    sensitivity = calibration.get("sensitivity_factor_db")
    cal_serial = str(calibration.get("serial_number") or "")
    serial_matches = bool(calibration.get("serial_matches_filename"))
    analog_gain = calibration.get("analog_gain_db")
    sensitivity_plausible = (
        sensitivity is not None
        and math.isfinite(float(sensitivity))
        and (
            profile is not _UMM6_PROFILE
            or -40.0 <= float(sensitivity) <= 0.0
        )
    )
    internal_gain_match = (
        _UMIK1_PROFILE.has_reference_internal_gain(selected)
        if profile is _UMIK1_PROFILE
        else True
        if profile is _UMM6_PROFILE
        else analog_gain is not None
        and abs(float(analog_gain) - _UMIK2_PROFILE.factory_analog_gain_db) <= 0.05
    )
    capture_gain = selected.get("capture_gain_db")
    capture_volume = selected.get("capture_volume_percent")
    gain_known = capture_gain is not None and capture_volume is not None
    capture_reference_match = gain_known and (
        abs(float(capture_gain) - float(profile.reference_capture_gain_db)) <= 0.05
        and abs(float(capture_volume) - float(profile.reference_capture_volume_percent)) <= 0.05
    ) if profile else False
    unique_umik = len(umik_inputs) == 1 and bool(selected) and str(umik_inputs[0].get("id") or "") == selected_id

    checks = {
        "selected_input_is_umik1": profile is _UMIK1_PROFILE,
        "unique_connected_umik1": unique_umik if profile is _UMIK1_PROFILE else False,
        "selected_input_is_umik2": profile is _UMIK2_PROFILE,
        "unique_connected_umik2": unique_umik if profile is _UMIK2_PROFILE else False,
        "selected_input_is_umm6": profile is _UMM6_PROFILE,
        "unique_connected_umm6": unique_umik if profile is _UMM6_PROFILE else False,
        "calibration_selected": bool(active_id and active),
        "sensitivity_factor_parsed": sensitivity_plausible,
        "calibration_serial_matches_filename": serial_matches,
        "calibration_curve_valid": calibration_curve_valid,
        "internal_gain_18_db": internal_gain_match,
        "capture_gain_known": gain_known,
        "capture_reference_state": capture_reference_match,
    }
    required_checks = (
        "selected_input_is_umik1", "unique_connected_umik1",
        "calibration_selected", "sensitivity_factor_parsed",
        "calibration_serial_matches_filename", "internal_gain_18_db",
        "capture_gain_known",
    ) if profile is _UMIK1_PROFILE else (
        "selected_input_is_umik2", "unique_connected_umik2",
        "calibration_selected", "sensitivity_factor_parsed",
        "calibration_serial_matches_filename", "internal_gain_18_db",
        "capture_gain_known", "capture_reference_state",
    ) if profile is _UMIK2_PROFILE else (
        "selected_input_is_umm6", "unique_connected_umm6",
        "calibration_selected", "sensitivity_factor_parsed",
        "calibration_serial_matches_filename", "calibration_curve_valid", "internal_gain_18_db",
        "capture_gain_known", "capture_reference_state",
    )
    available = bool(profile) and all(checks[key] for key in required_checks)
    if not selected_id:
        reason = "Select a supported UMIK measurement input first; manual C/Slow entry remains available."
    elif not selected:
        reason = "The selected measurement input is no longer available."
    elif not supported_model or not unique_umik:
        reason = "The selected input is not the uniquely identified supported UMIK USB capture device."
    elif not active:
        reason = f"Select the matching {supported_model} calibration file."
    elif not sensitivity_plausible or not cal_serial:
        reason = "The selected calibration file has no valid Sens Factor and SERNO."
    elif not serial_matches:
        reason = "The calibration SERNO does not match the selected calibration filename."
    elif profile is _UMM6_PROFILE and not calibration_curve_valid:
        reason = "The Dayton UMM-6 calibration file has no valid frequency/correction/phase curve."
    elif profile is _UMIK2_PROFILE and analog_gain is None:
        reason = "The selected UMIK-2 calibration file has no valid AGain."
    elif profile is _UMIK2_PROFILE and not internal_gain_match:
        reason = "The UMIK-2 calibration AGain does not match the 18 dB factory reference."
    elif not internal_gain_match:
        reason = "The UMIK-1 internal 18 dB reference gain cannot be verified."
    elif not gain_known:
        reason = f"The selected {supported_model} capture gain cannot be verified."
    elif profile is _UMIK2_PROFILE and not capture_reference_match:
        reason = "The UMIK-2 capture gain does not match the required 100% / 0 dB reference."
    elif profile is _UMM6_PROFILE and not capture_reference_match:
        reason = "The Dayton UMM-6 capture gain does not match the required +30 dB IPGA / 100% / 0 dB reference."
    else:
        reason = ""
    return {
        "available": available,
        "microphone_model": supported_model,
        "calibration_file_id": active_id,
        "calibration_filename": name,
        "calibration_path": str(path) if active else "",
        "sensitivity_factor_db": sensitivity,
        "analog_gain_db": analog_gain,
        "reference_sensitivity_dbfs_per_pa": (
            profile.reference_sensitivity_dbfs_per_pa
            if profile is _UMM6_PROFILE else None
        ),
        "reference_ipga_db": profile.reference_ipga_db if profile is _UMM6_PROFILE else None,
        "serial_number": cal_serial,
        "selected_input_id": selected_id,
        "selected_input": selected,
        "capture_gain_db": capture_gain,
        "capture_volume_percent": capture_volume,
        "reference_capture_gain_db": profile.reference_capture_gain_db if profile else None,
        "reference_capture_volume_percent": profile.reference_capture_volume_percent if profile else None,
        "checks": checks,
        "reason": reason,
    }


def _read_pcm16_channel(path: Path, channel_index: int) -> tuple[int, Any]:
    import numpy as spl_np

    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise RuntimeError("Automatic SPL capture did not produce 16-bit PCM")
        channels = wav.getnchannels()
        sample_rate = wav.getframerate()
        data = spl_np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(spl_np.float64)
    if channels < 1 or data.size < channels:
        raise RuntimeError("Automatic SPL capture is empty")
    selected = max(0, min(channels - 1, int(channel_index)))
    return sample_rate, data.reshape(-1, channels)[:, selected] / 32768.0


def _c_weighted_spl_from_capture(
    samples: Any,
    sample_rate: int,
    sensitivity_factor_db: float,
    calibration_path: Path,
    profile: Any = None,
) -> float:
    import numpy as spl_np

    signal = spl_np.asarray(samples, dtype=spl_np.float64)
    signal = signal - float(spl_np.mean(signal))
    if signal.size < sample_rate:
        raise RuntimeError("Automatic SPL capture is too short for stable averaging")
    window = spl_np.hanning(signal.size)
    coherent_power = float(spl_np.mean(spl_np.square(window)))
    spectrum = spl_np.fft.rfft(signal * window)
    frequencies = spl_np.fft.rfftfreq(signal.size, 1.0 / sample_rate)
    f2 = spl_np.square(frequencies)
    c_amplitude = (
        (12200.0 ** 2) * f2
        / spl_np.maximum((f2 + 20.6 ** 2) * (f2 + 12200.0 ** 2), 1e-30)
    ) * (10.0 ** (0.06 / 20.0))

    calibration_curve = (
        _UMM6_PROFILE.parse_calibration_curve(calibration_path)
        if profile is _UMM6_PROFILE
        else measurement_store._parse_calibration_file(calibration_path) if measurement_store else None
    )
    if calibration_curve is None:
        raise RuntimeError("The selected microphone calibration curve could not be parsed")
    cal_phases = None
    if profile is _UMM6_PROFILE:
        cal_freqs, cal_offsets, cal_phases = calibration_curve
    else:
        cal_freqs, cal_offsets = calibration_curve
    interpolated = spl_np.interp(
        spl_np.log(spl_np.maximum(frequencies, 1e-9)),
        spl_np.log(cal_freqs),
        cal_offsets,
        left=float(cal_offsets[0]),
        right=float(cal_offsets[-1]),
    )
    correction = spl_np.power(10.0, -interpolated / 20.0)
    if cal_phases is not None:
        interpolated_phase = spl_np.interp(
            spl_np.log(spl_np.maximum(frequencies, 1e-9)),
            spl_np.log(cal_freqs),
            cal_phases,
            left=float(cal_phases[0]),
            right=float(cal_phases[-1]),
        )
        correction = correction * spl_np.exp(-1j * spl_np.deg2rad(interpolated_phase))
    weighted = spectrum * c_amplitude * correction
    weighted_time = spl_np.fft.irfft(weighted, n=signal.size)
    rms = math.sqrt(float(spl_np.mean(spl_np.square(weighted_time))) / max(coherent_power, 1e-12))
    dbfs = 20.0 * math.log10(max(rms, 1e-12))
    # miniDSP/REW Sens Factor is referenced to 100 dB SPL with the UMIK
    # capture level at 100% (24 dB digital reference gain), hence +124 dB.
    if profile is _UMM6_PROFILE:
        # Dayton/REW Sens Factor is the individual dBFS reading at 94 dB SPL
        # with the UMM-6 at its maximum (+30 dB IPGA) capture reference.
        return dbfs + 94.0 - float(sensitivity_factor_db)
    return dbfs + 124.0 - float(sensitivity_factor_db)


def _calculate_spl_required_adjustment(measured_spl_db: float) -> float:
    measured = float(measured_spl_db)
    if not 40.0 <= measured <= 130.0:
        raise ValueError("Measured SPL must be between 40 and 130 dB")
    return 83.0 - measured


def _stop_spl_calibration_noise() -> None:
    global spl_calibration_noise_process, spl_calibration_restore_state
    process = spl_calibration_noise_process
    spl_calibration_noise_process = None
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
    restore = spl_calibration_restore_state
    spl_calibration_restore_state = None
    if restore and easyeffects_manager:
        try:
            easyeffects_manager.set_active_plugin_property(
                "autogain", 0, "target", restore["autogain_target"]
            )
            easyeffects_manager.set_active_plugin_property(
                "autogain", 0, "bypass", restore["autogain_bypass"]
            )
            easyeffects_manager.set_active_plugin_property(
                "loudness", 0, "outputGain", restore["loudness_output_gain"]
            )
            easyeffects_manager.set_active_plugin_property(
                "loudness", 0, "volume", restore["loudness_volume"]
            )
            if not restore["loudness_bypass"]:
                time.sleep(0.10)
            easyeffects_manager.set_active_plugin_property(
                "loudness", 0, "bypass", restore["loudness_bypass"]
            )
        finally:
            set_output_volume(restore["system_volume_percent"])


@app.get("/api/measurements/spl-calibration")
async def get_spl_calibration():
    extras = _require_easyeffects_manager().load_global_extras()
    return {
        "status": "ok",
        "target_spl_db": 83.0,
        "noise_lufs": -23.0,
        "meter_hint": "C-weighted / Slow",
        "output_profile": _spl_output_profile(),
        "loudness": extras["loudness"],
        "automatic": _spl_auto_capability(),
        "noise_active": bool(spl_calibration_noise_process and spl_calibration_noise_process.poll() is None),
    }


async def _release_spl_calibration_session() -> None:
    if measurement_sr_session is not None:
        await measurement_sr_session.unregister_spl_job(SPL_CALIBRATION_JOB_ID)


@app.post("/api/measurements/spl-calibration/noise")
async def set_spl_calibration_noise(request: Request):
    body = await request.json()
    enabled = bool(body.get("enabled"))
    if not enabled:
        _stop_spl_calibration_noise()
        await _release_spl_calibration_session()
        return {"status": "stopped"}
    if measurement_sr_session is not None:
        await measurement_sr_session.register_spl_job(SPL_CALIBRATION_JOB_ID)
    try:
        return _start_spl_calibration_noise()
    except Exception:
        await _release_spl_calibration_session()
        raise


def _start_spl_calibration_noise() -> dict[str, Any]:
    global spl_calibration_noise_process, spl_calibration_noise_file, spl_calibration_restore_state
    _stop_spl_calibration_noise()
    ee_manager = _require_easyeffects_manager()
    spl_calibration_restore_state = {
        "autogain_bypass": (
            ee_manager.get_active_plugin_property("autogain", 0, "bypass").lower()
            in {"true", "1", "on"}
        ),
        "autogain_target": float(
            ee_manager.get_active_plugin_property("autogain", 0, "target")
        ),
        "loudness_bypass": (
            ee_manager.get_active_plugin_property("loudness", 0, "bypass").lower()
            in {"true", "1", "on"}
        ),
        "loudness_volume": float(
            ee_manager.get_active_plugin_property("loudness", 0, "volume")
        ),
        "loudness_output_gain": float(
            ee_manager.get_active_plugin_property("loudness", 0, "outputGain")
        ),
        "system_volume_percent": get_output_volume(),
    }
    try:
        ee_manager.set_active_plugin_property("autogain", 0, "bypass", True)
        ee_manager.set_active_plugin_property("loudness", 0, "bypass", True)
        ee_manager.set_active_plugin_property("loudness", 0, "outputGain", 0.0)
        time.sleep(0.10)
        set_output_volume(100)
    except Exception:
        _stop_spl_calibration_noise()
        raise
    noise_path = Path(tempfile.gettempdir()) / "fxroute-spl-calibration-pink-noise.wav"
    if not noise_path.exists():
        generated = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "anoisesrc=color=pink:duration=60:sample_rate=48000",
                "-af", "loudnorm=I=-23:TP=-3:LRA=7,afade=t=in:st=0:d=0.5,afade=t=out:st=59.5:d=0.5",
                "-ac", "2", "-ar", "48000", str(noise_path),
            ],
            capture_output=True,
            text=True,
        )
        if generated.returncode != 0:
            _stop_spl_calibration_noise()
            raise HTTPException(status_code=500, detail=(generated.stderr or "Pink-noise generation failed").strip())
    spl_calibration_noise_file = noise_path
    try:
        spl_calibration_noise_process = subprocess.Popen(
            ["pw-play", "--volume=1.0", str(noise_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        _stop_spl_calibration_noise()
        raise
    return {"status": "playing", "settle_seconds": 1.0, "average_seconds": 3.0}


@app.post("/api/measurements/spl-calibration/automatic")
async def measure_spl_automatically():
    capability = _spl_auto_capability()
    if not capability["available"]:
        raise HTTPException(status_code=409, detail=capability["reason"])
    selected = capability["selected_input"]
    node_name = str(selected.get("node_name") or "")
    microphone_model = str(capability["microphone_model"])
    if not node_name:
        raise HTTPException(status_code=409, detail=f"The selected {microphone_model} PipeWire node is unavailable")

    capture_path = Path(tempfile.gettempdir()) / f"fxroute-spl-{microphone_model.lower()}-{uuid4().hex[:10]}.wav"
    settings = _read_measurement_setup_settings()
    channel_index = max(0, int(settings.get("selectedMicInputChannel") or "1") - 1)
    previous_volume = float(capability["capture_volume_percent"])
    recorder: subprocess.Popen[Any] | None = None
    if measurement_sr_session is not None:
        await measurement_sr_session.register_spl_job(SPL_CALIBRATION_JOB_ID)
    try:
        if abs(previous_volume - 100.0) > 0.05:
            subprocess.run(
                ["pactl", "set-source-volume", node_name, "100%"],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
        verified = subprocess.run(
            ["pactl", "get-source-volume", node_name],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        gain = re.search(r"/\s*([0-9.]+)%\s*/\s*([-+0-9.]+)\s*dB", verified.stdout or "")
        if not gain or abs(float(gain.group(1)) - 100.0) > 0.05 or abs(float(gain.group(2))) > 0.05:
            raise RuntimeError(f"The {microphone_model} capture gain could not be set to the 100% / 0 dB reference")

        recorder = subprocess.Popen(
            [
                "pw-record", "--target", node_name, "--rate", "48000",
                "--channels", "2", "--format", "s16", "--sample-count", str(5 * 48000),
                str(capture_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        await asyncio.sleep(0.2)
        _start_spl_calibration_noise()
        try:
            _stdout, stderr = await asyncio.to_thread(recorder.communicate, timeout=8)
        except subprocess.TimeoutExpired:
            recorder.kill()
            _stdout, stderr = recorder.communicate()
            raise RuntimeError(f"{microphone_model} automatic SPL capture timed out")
        if recorder.returncode != 0 and (not capture_path.exists() or capture_path.stat().st_size < 192044):
            raise RuntimeError(
                f"pw-record exited {recorder.returncode}: "
                f"{(stderr or f'{microphone_model} automatic SPL capture failed').strip()}"
            )

        try:
            sample_rate, samples = _read_pcm16_channel(capture_path, channel_index)
        except Exception as exc:
            raise RuntimeError(f"Could not read the {microphone_model} capture: {type(exc).__name__}: {exc}") from exc
        stable = samples[-3 * sample_rate:]
        try:
            measured = _c_weighted_spl_from_capture(
                stable,
                sample_rate,
                float(capability["sensitivity_factor_db"]),
                Path(capability["calibration_path"]),
                profile=(
                    _UMM6_PROFILE
                    if microphone_model == _UMM6_PROFILE.model
                    else None
                ),
            )
        except Exception as exc:
            raise RuntimeError(f"Could not calculate {microphone_model} SPL: {type(exc).__name__}: {exc}") from exc
        if not math.isfinite(measured) or not 40.0 <= measured <= 130.0:
            raise RuntimeError(f"Automatic {microphone_model} SPL result is outside the valid range ({measured:.1f} dB)")
        return {
            "status": "ok",
            "measured_spl_db": round(measured, 2),
            "required_adjustment_db": round(_calculate_spl_required_adjustment(measured), 2),
            "weighting": "C",
            "averaging_seconds": 3.0,
            "microphone_model": microphone_model,
            "serial_number": capability["serial_number"],
        }
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=f"{type(exc).__name__}: {exc}") from exc
    finally:
        if recorder and recorder.poll() is None:
            recorder.terminate()
        _stop_spl_calibration_noise()
        await _release_spl_calibration_session()
        if abs(previous_volume - 100.0) > 0.05:
            subprocess.run(
                ["pactl", "set-source-volume", node_name, f"{previous_volume:.4f}%"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        capture_path.unlink(missing_ok=True)


@app.post("/api/measurements/spl-calibration/apply")
async def apply_spl_calibration(request: Request):
    global spl_calibration_restore_state
    body = await request.json()
    measured = float(body.get("measured_spl_db"))
    try:
        adjustment = _calculate_spl_required_adjustment(measured)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _stop_spl_calibration_noise()
    await _release_spl_calibration_session()
    ee_manager = _require_easyeffects_manager()
    extras = ee_manager.load_global_extras()
    profile = _spl_output_profile()
    automatic = _spl_auto_capability()
    extras["loudness"]["params"]["calibration"] = {
        "outputProfileId": profile["id"],
        "outputProfileLabel": profile["label"],
        "targetSplDb": 83.0,
        "measuredSplDb": measured,
        "requiredAdjustmentDb": adjustment,
        "calibrated": abs(adjustment) <= 1.0,
        "method": "automatic" if automatic["available"] else "manual",
        "microphoneModel": automatic["microphone_model"],
        "calibrationFileId": automatic["calibration_file_id"],
        "calibrationFilename": automatic["calibration_filename"],
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    profiles = dict(extras["loudness"]["params"].get("calibrationProfiles") or {})
    profiles[profile["id"]] = dict(extras["loudness"]["params"]["calibration"])
    extras["loudness"]["params"]["calibrationProfiles"] = profiles
    saved = ee_manager.apply_autogain_loudness_runtime(
        ee_manager.load_global_extras(), extras, persist_all_presets=False
    )["extras"]
    spl_calibration_restore_state = None
    return {
        "status": "ok",
        "required_adjustment_db": adjustment,
        "calibrated": abs(adjustment) <= 1.0,
        "calibration": saved["loudness"]["params"]["calibration"],
    }

@app.get("/api/easyeffects/presets")
async def list_easyeffects_presets():
    return _require_easyeffects_manager().get_status()


@app.get("/api/easyeffects/presets/{preset_name}/file")
async def download_easyeffects_preset_file(preset_name: str):
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")
    preset = next((item for item in easyeffects_manager.list_presets() if item.get("name") == preset_name), None)
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")
    preset_path = Path(str(preset.get("path") or "")).resolve()
    if not _path_within_root(preset_path, easyeffects_manager.output_dir):
        raise HTTPException(status_code=403, detail="Preset path outside EasyEffects preset directory")
    if not preset_path.is_file():
        raise HTTPException(status_code=404, detail="Preset file missing")
    try:
        payload = json.loads(preset_path.read_text())
    except Exception:
        payload = None
    kernel_names = easyeffects_manager._extract_kernel_names_from_payload(payload) if isinstance(payload, dict) else set()
    ir_paths = []
    for kernel_name in sorted(kernel_names):
        ir_paths.extend(easyeffects_manager._find_ir_paths_for_kernel_name(kernel_name))
    if ir_paths:
        with tempfile.NamedTemporaryFile(prefix="fxroute-preset-", suffix=".zip", delete=False) as temp_file:
            temp_zip_path = Path(temp_file.name)
        used_names = set()
        try:
            with zipfile.ZipFile(temp_zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.write(preset_path, arcname="preset.json")
                for ir_path in ir_paths:
                    if ir_path.is_file() and _path_within_root(ir_path.resolve(), easyeffects_manager.irs_dir):
                        archive.write(ir_path, arcname=_dedupe_archive_name(ir_path.name, used_names))
                        archive.write(ir_path, arcname=_dedupe_archive_name(f"{ir_path.stem}.wav", used_names))
                manifest = {
                    "type": "fxroute-preset-bundle",
                    "version": 1,
                    "preset": preset_path.name,
                    "irs": [path.name for path in ir_paths if path.is_file()],
                }
                archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        except Exception:
            temp_zip_path.unlink(missing_ok=True)
            raise
        return FileResponse(
            temp_zip_path,
            filename=f"{preset_path.stem}.zip",
            media_type="application/zip",
            background=BackgroundTask(_cleanup_temp_file, temp_zip_path),
        )
    return FileResponse(preset_path, filename=preset_path.name)

def _normalize_measurement_optional_input_channel(value: Any) -> str:
    return normalize_measurement_optional_input_channel(value)


def _measurement_setup_settings_from_payload(settings: dict[str, Any]) -> dict[str, Any]:
    return measurement_setup_settings_from_payload(settings)


def _read_measurement_setup_settings() -> dict[str, Any]:
    path = getattr(measurement_store, "settings_path", None)
    if not path:
        return _measurement_setup_settings_from_payload({})
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        settings = payload if isinstance(payload, dict) else {}
    except Exception:
        settings = {}
    return _measurement_setup_settings_from_payload(settings)


def _update_measurement_setup_settings(patch: dict[str, Any]) -> dict[str, Any]:
    path = getattr(measurement_store, "settings_path", None)
    if not path:
        return _measurement_setup_settings_from_payload({})
    settings_path = Path(path)
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        settings = payload if isinstance(payload, dict) else {}
    except Exception:
        settings = {}
    measure_settings = settings.setdefault("measure", {})
    if not isinstance(measure_settings, dict):
        measure_settings = {}
        settings["measure"] = measure_settings

    if "selectedInputId" in patch or "input_id" in patch:
        measure_settings["selectedInputId"] = str(patch.get("selectedInputId", patch.get("input_id")) or "").strip()
    if "selectedMicInputChannel" in patch or "mic_input_channel" in patch:
        raw_mic = patch.get("selectedMicInputChannel", patch.get("mic_input_channel"))
        measure_settings["selectedMicInputChannel"] = _normalize_measurement_optional_input_channel(raw_mic) or "1"
    if "selectedReferenceInputChannel" in patch or "reference_input_channel" in patch:
        raw_reference = patch.get("selectedReferenceInputChannel", patch.get("reference_input_channel"))
        measure_settings["selectedReferenceInputChannel"] = _normalize_measurement_optional_input_channel(raw_reference)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return _measurement_setup_settings_from_payload(settings)


@app.get("/api/measurements")
async def list_measurements():
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    payload = measurement_store.list_measurements()
    payload["measurement_settings"] = _read_measurement_setup_settings()
    return payload


@app.get("/api/measurements/settings")
async def get_measurement_settings():
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    return {
        "status": "ok",
        "measurement_settings": _read_measurement_setup_settings(),
    }


@app.patch("/api/measurements/settings")
async def update_measurement_settings(request: Request):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Measurement settings payload must be an object")
    return {
        "status": "ok",
        "measurement_settings": _update_measurement_setup_settings(body),
    }

@app.get("/api/measurements/inputs")
async def list_measurement_inputs():
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    return measurement_store.list_inputs()


@app.post("/api/measurements/calibrations")
async def upload_measurement_calibration(calibration_file: UploadFile = File(...)):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    filename = calibration_file.filename or "calibration.txt"
    data = await calibration_file.read()
    try:
        return measurement_store.upload_calibration_file(filename, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/measurements/calibrations/{calibration_id}/export")
async def export_measurement_calibration(calibration_id: str):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        path, filename = measurement_store.get_calibration_file_for_export(calibration_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Calibration file not found")
    return FileResponse(path, filename=filename, media_type="text/plain")


@app.patch("/api/measurements/calibrations/active")
async def set_active_measurement_calibration(request: Request):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    calibration_ref = payload.get("calibration_file_id") if isinstance(payload, dict) else ""
    return measurement_store.set_active_calibration_file_id(str(calibration_ref or ""))


@app.delete("/api/measurements/calibrations/{calibration_id}")
async def delete_measurement_calibration(calibration_id: str):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        return measurement_store.delete_calibration_file(calibration_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Calibration file not found")


@app.post("/api/measurements/house-curves")
async def upload_measurement_house_curve(house_curve_file: UploadFile = File(...)):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    filename = house_curve_file.filename or "house-curve.txt"
    data = await house_curve_file.read()
    try:
        return measurement_store.upload_house_curve_file(filename, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/measurements/house-curves/{house_curve_id}/export")
async def export_measurement_house_curve(house_curve_id: str):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        path, filename = measurement_store.get_house_curve_file_for_export(house_curve_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="House curve file not found")
    return FileResponse(path, filename=filename, media_type="text/plain")


@app.delete("/api/measurements/house-curves/{house_curve_id}")
async def delete_measurement_house_curve(house_curve_id: str):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        return measurement_store.delete_house_curve_file(house_curve_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="House curve file not found")


@app.get("/api/measurements/{measurement_id}/file")
async def download_measurement_file(measurement_id: str):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    measurement = next((item for item in measurement_store.list_measurements().get("measurements", []) if item.get("id") == measurement_id), None)
    if not measurement:
        raise HTTPException(status_code=404, detail="Measurement not found")
    storage_path = Path(str(measurement.get("storage_path") or "")).resolve()
    if not _path_within_root(storage_path, measurement_store.measurements_dir):
        raise HTTPException(status_code=403, detail="Measurement path outside measurement storage")
    if not storage_path.is_file():
        raise HTTPException(status_code=404, detail="Measurement file missing")
    return FileResponse(storage_path, filename=storage_path.name)

@app.get("/api/certificate/local-root")
async def download_local_root_certificate():
    cert_path = Path("/etc/fxroute/certs/fxroute-local-root.crt")
    if not cert_path.exists():
        raise HTTPException(status_code=404, detail="Local root certificate not available on this host")
    return FileResponse(cert_path, filename="fxroute-local-root.crt", media_type="application/x-x509-ca-cert")

@app.post("/api/measurements/start")
async def start_measurement(
    input_id: str = Form(...),
    channel: str = Form("left"),
    mic_input_channel: str = Form("1"),
    reference_input_channel: str = Form(""),
    calibration_ref: str = Form(""),
    calibration_file: Optional[UploadFile] = File(None),
):
    global measurement_store, _auto_sub_lock
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    if _auto_sub_lock and _auto_sub_lock.locked():
        raise HTTPException(status_code=423, detail="Auto Sub Optimize is in progress")

    calibration_bytes = None
    calibration_filename = None
    if calibration_file is not None:
        calibration_filename = calibration_file.filename or "calibration.txt"
        calibration_bytes = await calibration_file.read()

    measurement_rate = _resolve_measurement_start_sample_rate()
    pending_job_id = f"pending:{uuid4()}"
    sweep_gen = measurement_sr_session.generation if measurement_sr_session is not None else 0
    try:
        if measurement_sr_session is not None:
            sweep_gen = await measurement_sr_session.register_manual_job(pending_job_id)
        await _measurement_entry_preflight(measurement_rate)
        job = await measurement_store.start_measurement(
            input_id=input_id,
            channel=channel,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            calibration_ref=calibration_ref,
        )
        if measurement_sr_session is not None:
            await measurement_sr_session.replace_manual_job(pending_job_id, job["id"])
            asyncio.create_task(_unregister_measurement_job_after_completion(job["id"], sweep_gen))
    except ValueError as exc:
        if measurement_sr_session is not None:
            await measurement_sr_session.unregister_manual_job(pending_job_id)
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        if measurement_sr_session is not None:
            await measurement_sr_session.unregister_manual_job(pending_job_id)
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "ok", "job": job}

@app.post("/api/measurements/lr-repeat/start")
async def start_lr_repeat_measurement(
    input_id: str = Form(...),
    base_name: str = Form(""),
    mic_input_channel: str = Form("1"),
    reference_input_channel: str = Form(""),
    calibration_ref: str = Form(""),
    calibration_file: Optional[UploadFile] = File(None),
):
    global measurement_store, _auto_sub_lock
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    if _auto_sub_lock and _auto_sub_lock.locked():
        raise HTTPException(status_code=423, detail="Auto Sub Optimize is in progress")

    calibration_bytes = None
    calibration_filename = None
    if calibration_file is not None:
        calibration_filename = calibration_file.filename or "calibration.txt"
        calibration_bytes = await calibration_file.read()

    measurement_rate = _resolve_measurement_start_sample_rate()
    pending_job_id = f"pending:{uuid4()}"
    sweep_gen = measurement_sr_session.generation if measurement_sr_session is not None else 0
    try:
        if measurement_sr_session is not None:
            sweep_gen = await measurement_sr_session.register_manual_job(pending_job_id)
        await _measurement_entry_preflight(measurement_rate)
        job = await measurement_store.start_lr_repeat_measurement(
            input_id=input_id,
            base_name=base_name,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            calibration_ref=calibration_ref,
        )
        if measurement_sr_session is not None:
            await measurement_sr_session.replace_manual_job(pending_job_id, job["id"])
            asyncio.create_task(_unregister_measurement_job_after_completion(job["id"], sweep_gen))
    except ValueError as exc:
        if measurement_sr_session is not None:
            await measurement_sr_session.unregister_manual_job(pending_job_id)
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        if measurement_sr_session is not None:
            await measurement_sr_session.unregister_manual_job(pending_job_id)
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "ok", "job": job}

@app.get("/api/measurements/jobs/{job_id}")
async def get_measurement_job(job_id: str):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        job = measurement_store.get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Measurement job not found")
    return {"status": "ok", "job": job}

@app.post("/api/measurements/jobs/{job_id}/cancel")
async def cancel_measurement_job(job_id: str):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        job = measurement_store.cancel_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Measurement job not found")
    return {"status": "ok", "job": job}

@app.post("/api/measurements/save")
async def save_measurement(request: Request):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")

    try:
        body = await request.json()
    except Exception:
        logger.exception("Measurement save request failed: invalid JSON body")
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    measurement_id = body.get("id") if isinstance(body, dict) else ""
    measurement_name = body.get("name") if isinstance(body, dict) else ""
    measurements = body.get("measurements") if isinstance(body, dict) else None
    trace_count = len(body.get("traces") or []) if isinstance(body, dict) and isinstance(body.get("traces"), list) else 0
    audio_output_context = _build_measurement_audio_output_context()
    if isinstance(body, dict) and not body.get("audio_output_context"):
        body["audio_output_context"] = audio_output_context

    logger.info(
        "Measurement save request received: id=%s name=%s traces=%s audio_output_mode=%s",
        measurement_id,
        measurement_name,
        trace_count,
        audio_output_context.get("output_mode", "unknown"),
    )
    try:
        if isinstance(measurements, list):
            for item in measurements:
                if isinstance(item, dict) and not item.get("audio_output_context"):
                    item["audio_output_context"] = audio_output_context
            saved_measurements = measurement_store.save_measurements(measurements)
            logger.info("Measurement set save completed: count=%s", len(saved_measurements))
            return {"status": "ok", "measurements": saved_measurements}
        saved = measurement_store.save_measurement(body)
    except ValueError as exc:
        logger.warning("Measurement save rejected: id=%s error=%s", measurement_id, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Measurement save failed: id=%s name=%s", measurement_id, measurement_name)
        raise
    logger.info(
        "Measurement save completed: id=%s name=%s",
        saved.get("id") if isinstance(saved, dict) else "",
        saved.get("name") if isinstance(saved, dict) else "",
    )
    return {"status": "ok", "measurement": saved}

@app.post("/api/measurements/merge")
async def merge_measurements(request: Request):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    measurement_ids = body.get("measurementIds", body.get("measurement_ids")) if isinstance(body, dict) else []
    name = body.get("name") if isinstance(body, dict) else ""
    if not isinstance(measurement_ids, list):
        raise HTTPException(status_code=400, detail="measurementIds must be an array")

    try:
        merged = measurement_store.merge_measurements(measurement_ids, str(name or ""))
    except KeyError:
        raise HTTPException(status_code=404, detail="Measurement not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Measurement merge failed: ids=%s name=%s", measurement_ids, name)
        raise HTTPException(status_code=500, detail="Failed to merge selected measurements")
    return {"status": "ok", "measurement": merged}

@app.delete("/api/measurements/{measurement_id}")
async def delete_measurement(measurement_id: str):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")

    try:
        measurement_store.delete_measurement(measurement_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Measurement not found")
    return {"status": "ok", "deleted": measurement_id}


# ---------------------------------------------------------------------------
# Auto Sub Optimize
# ---------------------------------------------------------------------------

_AUTO_SUB_JOBS: dict[str, dict[str, Any]] = {}
_auto_sub_lock: asyncio.Lock = asyncio.Lock()
_AUTO_SUB_MAX_CALIBRATION_BYTES: int = 2 * 1024 * 1024  # 2 MiB


def _cleanup_stale_autosub_cancelling_jobs() -> None:
    """Promote stale cancelling AutoSub jobs when the lock is not held.

    If the lock can be acquired immediately, no worker is actively running,
    so any cancelling job is stale and can be marked cancelled.
    """
    try:
        acquired = _auto_sub_lock and _auto_sub_lock.locked()
    except RuntimeError:
        acquired = False
    if acquired:
        return  # Lock held, worker still active

    for job_id, job in list(_AUTO_SUB_JOBS.items()):
        if str(job.get("status") or "").lower() != "cancelling":
            continue
        logger.warning(
            "AUTOSUB stale running state recovered: job_id=%s status=cancelling->cancelled",
            job_id,
        )
        job["status"] = "cancelled"
        job["message"] = "Auto Sub Optimize cancelled."
        job["cancel_requested"] = True
        job["cancelled_at"] = job.get("cancelled_at") or datetime.now(timezone.utc).isoformat()


def _auto_sub_cancel_requested(job: dict[str, Any]) -> bool:
    status = str(job.get("status") or "").lower()
    return bool(job.get("cancel_requested")) or status in ("cancelled", "cancelling")


def _auto_sub_cancelled_candidate(delay_ms: float, stage: str) -> dict[str, Any]:
    return {
        "delay_ms": delay_ms,
        "name": str(delay_ms),
        "points": [],
        "sweep_id": "",
        "status": "cancelled",
        "error": "Auto Sub Optimize cancelled",
        "scan": stage,
    }


async def _restore_auto_sub_original_config(original_config_snapshot: dict[str, Any]) -> None:
    """Restore subwoofer config from snapshot."""
    try:
        from samplerate import set_audio_output_mode
        mode = original_config_snapshot.get("mode", "stereo") or "stereo"
        subwoofer_config = (
            _auto_sub_22_global_config(original_config_snapshot)
            if mode in OUTPUT_MODE_SUBWOOFER_22_MODES
            else original_config_snapshot.get("subwoofer") or {}
        )
        set_audio_output_mode(
            mode,
            subwoofer_config,
            original_config_snapshot.get("subwoofers") or {},
        )
        if subwoofer_runtime is not None:
            config = SubwooferRuntimeConfig.from_overview(get_audio_output_overview())
            await subwoofer_runtime.sync(config)
    except Exception:
        logger.exception("Auto-sub: failed to restore original config from snapshot")


def _auto_sub_step_ms(fc: int) -> float:
    return (1000.0 / float(fc)) / 16.0


def _auto_sub_clamped_delay(delay_ms: float) -> float:
    return round(max(-40.0, min(40.0, float(delay_ms))), 2)


def _auto_sub_snapshot_copy(mode_state: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(mode_state))
    except Exception:
        return dict(mode_state)


def _auto_sub_22_global_config(snapshot: dict[str, Any]) -> dict[str, Any]:
    subwoofer = snapshot.get("subwoofer") if isinstance(snapshot.get("subwoofer"), dict) else {}
    return {
        "crossover_frequency_hz": snapshot.get("crossover_frequency_hz", subwoofer.get("crossover_frequency_hz", 80)),
        "main_highpass_enabled": snapshot.get("main_highpass_enabled", subwoofer.get("main_highpass_enabled", True)),
    }


def _auto_sub_22_sub(snapshot: dict[str, Any], sub_key: str) -> dict[str, Any]:
    subwoofers = snapshot.get("subwoofers") if isinstance(snapshot.get("subwoofers"), dict) else {}
    sub = subwoofers.get(sub_key) if isinstance(subwoofers.get(sub_key), dict) else {}
    return {
        "level_db": float(sub.get("level_db", 0.0) or 0.0),
        "alignment_ms": _auto_sub_clamped_delay(float(sub.get("alignment_ms", 0.0) or 0.0)),
        "polarity": str(sub.get("polarity", "normal") or "normal"),
    }


def _auto_sub_22_candidate_subwoofers(
    snapshot: dict[str, Any],
    *,
    sub1_alignment_ms: float,
    sub2_alignment_ms: float,
    active_subs: tuple[str, ...],
    sub1_polarity: str | None = None,
    sub2_polarity: str | None = None,
) -> dict[str, Any]:
    sub1 = _auto_sub_22_sub(snapshot, "sub1")
    sub2 = _auto_sub_22_sub(snapshot, "sub2")
    sub1["alignment_ms"] = _auto_sub_clamped_delay(sub1_alignment_ms)
    sub2["alignment_ms"] = _auto_sub_clamped_delay(sub2_alignment_ms)
    if sub1_polarity is not None:
        sub1["polarity"] = "invert" if sub1_polarity == "invert" else "normal"
    if sub2_polarity is not None:
        sub2["polarity"] = "invert" if sub2_polarity == "invert" else "normal"
    if "sub1" not in active_subs:
        sub1["level_db"] = -80.0
    if "sub2" not in active_subs:
        sub2["level_db"] = -80.0
    return {"sub1": sub1, "sub2": sub2}


def _auto_sub_22_verify_alignment(mode_state: dict[str, Any], sub1_alignment_ms: float, sub2_alignment_ms: float) -> bool:
    subwoofers = mode_state.get("subwoofers") if isinstance(mode_state.get("subwoofers"), dict) else {}
    sub1 = subwoofers.get("sub1") if isinstance(subwoofers.get("sub1"), dict) else {}
    sub2 = subwoofers.get("sub2") if isinstance(subwoofers.get("sub2"), dict) else {}
    try:
        return (
            abs(float(sub1.get("alignment_ms", -9999)) - _auto_sub_clamped_delay(sub1_alignment_ms)) <= 0.001
            and abs(float(sub2.get("alignment_ms", -9999)) - _auto_sub_clamped_delay(sub2_alignment_ms)) <= 0.001
        )
    except (TypeError, ValueError):
        return False


def _auto_sub_opposite_polarity(polarity: str) -> str:
    return "normal" if str(polarity).lower() == "invert" else "invert"


def _auto_sub_polarity_decision(
    incumbent: dict[str, Any], alternative: dict[str, Any], *, min_score_gain: float = 0.03,
) -> dict[str, Any]:
    """Protect the active polarity unless a measured alternative is clearly better."""
    incumbent_score = _auto_sub_score_value(incumbent)
    alternative_score = _auto_sub_score_value(alternative)
    gain = alternative_score - incumbent_score
    accepted = gain >= min_score_gain
    return {
        "accepted": accepted,
        "score_gain": round(gain, 4),
        "min_score_gain": min_score_gain,
        "reason": "alternative_clearly_better" if accepted else "incumbent_protected_unclear_advantage",
    }


def _auto_sub_22_name(sub1_alignment_ms: float, sub2_alignment_ms: float) -> str:
    return f"Sub1 {sub1_alignment_ms:.2f} ms / Sub2 {sub2_alignment_ms:.2f} ms"


def _auto_sub_22_stereo_name(left_alignment_ms: float, right_alignment_ms: float) -> str:
    return f"Left {left_alignment_ms:.2f} ms / Right {right_alignment_ms:.2f} ms"


def _auto_sub_direct_neighbors(delay_a: float, delay_b: float, scan_delays: list[float]) -> bool:
    sorted_delays = sorted(float(delay) for delay in scan_delays)
    tolerance = 0.05
    for left, right in zip(sorted_delays, sorted_delays[1:]):
        if abs(left - float(delay_a)) <= tolerance and abs(right - float(delay_b)) <= tolerance:
            return True
        if abs(right - float(delay_a)) <= tolerance and abs(left - float(delay_b)) <= tolerance:
            return True
    return False


def _auto_sub_fine_delay_candidates(
    winner: dict[str, Any],
    runner_up: dict[str, Any] | None,
    step_ms: float,
    existing_delays: set[float],
) -> list[float]:
    """Generate 4-6 fine delays around the coarse winner area."""
    winner_delay = float(winner.get("delay_ms", 0.0))
    fine_step = step_ms / 4.0
    offsets: list[float] = []

    # Always sample winner +/- 0.25 and +/- 0.5 coarse step.
    offsets.extend([-2.0 * fine_step, -fine_step, fine_step, 2.0 * fine_step])

    if runner_up is not None:
        runner_delay = float(runner_up.get("delay_ms", winner_delay))
        delta = runner_delay - winner_delay
        if 0.05 < abs(delta) <= (step_ms + 0.05):
            # Cover the interval and the runner-up neighbourhood without
            # exceeding the 4-6 candidate target after de-duplication.
            offsets.extend([
                delta * 0.5,
                delta - fine_step,
                delta + fine_step,
                delta * 0.25,
                delta * 0.75,
            ])

    candidates: list[float] = []
    existing = {round(float(delay), 2) for delay in existing_delays}
    for offset in sorted(offsets, key=lambda value: (abs(value), value)):
        delay = _auto_sub_clamped_delay(winner_delay + offset)
        if any(abs(delay - existing_delay) <= 0.05 for existing_delay in existing):
            continue
        if all(abs(delay - candidate) > 0.05 for candidate in candidates):
            candidates.append(delay)
            existing.add(round(delay, 2))
        if len(candidates) >= 6:
            break

    return candidates


def _auto_sub_fine_trigger_reasons(
    scoring: dict[str, Any],
    scan_delays: list[float],
) -> list[str]:
    reasons: list[str] = []
    winner = scoring.get("winner") or {}
    runner_up = scoring.get("runner_up")

    if scoring.get("confidence") == "uncertain":
        reasons.append("uncertain coarse confidence")

    if runner_up:
        winner_score = float(winner.get("score_pct", 0.0) or 0.0)
        runner_score = float(runner_up.get("score_pct", 0.0) or 0.0)
        if winner_score - runner_score < 5.0:
            reasons.append("winner/runner-up margin below 5 percentage points")
        if _auto_sub_direct_neighbors(
            float(winner.get("delay_ms", 0.0)),
            float(runner_up.get("delay_ms", 0.0)),
            scan_delays,
        ):
            reasons.append("winner and runner-up are direct coarse neighbours")

    return reasons


def _auto_sub_rank_results(results: list[dict[str, Any]]) -> None:
    for rank, result in enumerate(results, start=1):
        result["rank"] = rank


def _auto_sub_has_points(result: dict[str, Any], key: str = "points") -> bool:
    points = result.get(key) or []
    return isinstance(points, list) and len(points) >= 3


def _auto_sub_delay_key(result: dict[str, Any]) -> float:
    return round(float(result.get("delay_ms", 0.0)), 2)


def _auto_sub_score_value(result: dict[str, Any] | None) -> float:
    if not result:
        return float("-inf")
    try:
        return float(result.get("final_score", result.get("score", 0.0)) or 0.0)
    except (TypeError, ValueError):
        return float("-inf")


def _auto_sub_best_scan_result(results: list[dict[str, Any]], scan: str) -> dict[str, Any] | None:
    matches = [result for result in results if str(result.get("scan") or "coarse") == scan]
    if not matches:
        return None
    return max(matches, key=_auto_sub_score_value)


def _auto_sub_result_for_delay(results: list[dict[str, Any]], delay_ms: float) -> dict[str, Any] | None:
    delay_key = round(float(delay_ms), 2)
    for result in results:
        if round(float(result.get("delay_ms", 0.0)), 2) == delay_key:
            return result
    return None


def _auto_sub_shared_bass_offset(
    *point_sets: list,
    low_hz: float = 20.0,
    high_hz: float = 200.0,
) -> float:
    """Compute a single median dB offset from combined bass-region points.

    Used so that all traces within one AutoSub run share the same vertical
    reference, preserving Before/After and L/R relative level differences.
    """
    bass_dbs: list[float] = []
    for pts in point_sets:
        if not isinstance(pts, list):
            continue
        for p in pts:
            if not (isinstance(p, (list, tuple)) and len(p) >= 2):
                continue
            try:
                hz, db = float(p[0]), float(p[1])
                if low_hz <= hz <= high_hz:
                    bass_dbs.append(db)
            except (ValueError, TypeError):
                continue
    if not bass_dbs:
        return 0.0
    sorted_dbs = sorted(bass_dbs)
    mid = len(sorted_dbs) // 2
    return sorted_dbs[mid] if len(sorted_dbs) % 2 == 1 else (sorted_dbs[mid - 1] + sorted_dbs[mid]) / 2.0


def _validate_auto_sub_target_curve_snapshot(raw_snapshot: str) -> tuple[dict[str, Any] | None, str | None]:
    """Validate and detach the browser-selected Target Curve for one AutoSub job."""
    if not str(raw_snapshot or "").strip():
        return None, "Target Curve snapshot is missing"
    try:
        incoming = json.loads(raw_snapshot)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "Target Curve snapshot is not valid JSON"
    if not isinstance(incoming, dict):
        return None, "Target Curve snapshot must be an object"
    key = str(incoming.get("key") or "").strip()
    label = str(incoming.get("label") or "").strip()
    provenance = str(incoming.get("provenance") or "").strip()
    if not key or not label:
        return None, "Target Curve key and label are required"
    if provenance not in {"built_in", "uploaded"}:
        return None, "Target Curve provenance must be built_in or uploaded"
    raw_points = incoming.get("points")
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        return None, "Target Curve requires at least two points"
    points: list[list[float]] = []
    previous_frequency = 0.0
    for index, point in enumerate(raw_points):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None, f"Target Curve point {index + 1} must be [frequency_hz, db]"
        try:
            frequency_hz, db = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            return None, f"Target Curve point {index + 1} must contain numbers"
        if not math.isfinite(frequency_hz) or not math.isfinite(db):
            return None, f"Target Curve point {index + 1} must contain finite numbers"
        if frequency_hz <= 0:
            return None, f"Target Curve point {index + 1} frequency must be greater than zero"
        if frequency_hz <= previous_frequency:
            return None, "Target Curve frequencies must be strictly increasing"
        points.append([frequency_hz, db])
        previous_frequency = frequency_hz
    return {"key": key, "label": label, "provenance": provenance, "points": points}, None


def _auto_sub_measurement_from_sweep(
    sweep_result: dict[str, Any],
    label: str,
    name: str,
    offset_db: float | None = None,
) -> dict[str, Any]:
    """Convert an AutoSub sweep result into a frontend-compatible measurement dict.

    When *offset_db* is provided it is used as the shared vertical reference
    for all traces.  When omitted, the shared offset is computed from the
    combined bass-region (20‑200 Hz) dB values of the L + R points inside
    *sweep_result*, which is the correct default for a single-sweep call
    (2.1 / 2.2 Mono).  Callers that need a cross-sweep shared offset
    (2.2 Stereo) can pre-compute it and pass it explicitly.
    """
    traces: list[dict[str, Any]] = []
    base_id = uuid4().hex[:12]

    left_points = sweep_result.get("points_left") or []
    right_points = sweep_result.get("points_right") or []

    if offset_db is None:
        offset_db = _auto_sub_shared_bass_offset(left_points, right_points)

    if isinstance(left_points, list) and len(left_points) >= 3:
        points = [[float(p[0]), float(p[1]) - offset_db] for p in left_points]
        traces.append({
            "kind": "measured",
            "label": f"{label} L",
            "role": "left",
            "points": points,
        })

    if isinstance(right_points, list) and len(right_points) >= 3:
        points = [[float(p[0]), float(p[1]) - offset_db] for p in right_points]
        traces.append({
            "kind": "measured",
            "label": f"{label} R",
            "role": "right",
            "points": points,
        })

    return {
        "id": f"autosub-{base_id}",
        "name": name,
        "traces": traces,
    }


def _auto_sub_select_accepted_winner(
    *,
    coarse_winner: dict[str, Any],
    fine_winner: dict[str, Any] | None,
    incumbent_winner: dict[str, Any] | None,
    score_epsilon: float = 0.001,
) -> dict[str, Any]:
    protected_winner = coarse_winner
    if incumbent_winner is not None and (
        _auto_sub_score_value(incumbent_winner) + score_epsilon >= _auto_sub_score_value(coarse_winner)
    ):
        protected_winner = incumbent_winner

    accepted_winner = protected_winner
    fine_accepted = False
    reject_reason = None

    if fine_winner is None:
        reject_reason = "fine_not_better"
    elif _auto_sub_score_value(fine_winner) <= _auto_sub_score_value(protected_winner) + score_epsilon:
        reject_reason = "incumbent_better" if protected_winner is incumbent_winner else "fine_not_better"
    else:
        fine_xo_loss = max(
            0.0,
            float(coarse_winner.get("xo_score", 0.0) or 0.0) - float(fine_winner.get("xo_score", 0.0) or 0.0),
        )
        fine_timing_loss = max(
            0.0,
            float(coarse_winner.get("timing_band_score", 0.0) or 0.0)
            - float(fine_winner.get("timing_band_score", 0.0) or 0.0),
        )
        low_guard_gain_db = max(
            0.0,
            float(coarse_winner.get("low_guard_loss_db", 0.0) or 0.0)
            - float(fine_winner.get("low_guard_loss_db", 0.0) or 0.0),
        )
        if low_guard_gain_db <= 1.0 and (fine_xo_loss >= 0.05 or fine_timing_loss >= 0.05):
            reject_reason = "xo_loss_vs_coarse"
        else:
            accepted_winner = fine_winner
            fine_accepted = True

    return {
        "accepted_winner": accepted_winner,
        "fine_accepted": fine_accepted,
        "reject_reason": reject_reason,
        "protected_winner": protected_winner,
        "incumbent_winner": incumbent_winner,
        "incumbent_score": round(_auto_sub_score_value(incumbent_winner), 4) if incumbent_winner else None,
    }


def _auto_sub_scoring_confidence(results: list[dict[str, Any]]) -> str:
    if len(results) < 2:
        return "uncertain"
    winner = results[0]
    runner_up = results[1]
    winner_score = float(winner.get("score", 0.0) or 0.0)
    if winner_score <= 0:
        return "uncertain"
    margin = (winner_score - float(runner_up.get("score", 0.0) or 0.0)) / winner_score
    if margin > 0.15:
        return "clear"
    if margin > 0.05:
        return "close"
    return "uncertain"


def _auto_sub_score_single_channel_fallback(
    candidates: list[dict[str, Any]],
    *,
    crossover_hz: int,
    channel_name: str,
    low_guard_reference_delay_ms: float | None = None,
) -> dict[str, Any]:
    scoring = score_sub_alignment_candidates(
        candidates,
        crossover_hz=crossover_hz,
        low_guard_reference_delay_ms=low_guard_reference_delay_ms,
    )
    scan_by_delay = {_auto_sub_delay_key(candidate): candidate.get("scan", "coarse") for candidate in candidates}
    for result in scoring.get("results", []):
        result["scan"] = scan_by_delay.get(_auto_sub_delay_key(result), result.get("scan", "coarse"))
        result["score_source"] = channel_name
        score = round(float(result.get("score", 0.0) or 0.0), 4)
        score_pct = round(score * 100.0, 1)
        result.setdefault("score", score)
        result.setdefault("score_pct", score_pct)
        if channel_name == "left":
            result["score_L"] = score
            result["score_L_pct"] = score_pct
            result["score_R"] = None
            result["score_R_pct"] = None
        else:
            result["score_L"] = None
            result["score_L_pct"] = None
            result["score_R"] = score
            result["score_R_pct"] = score_pct
    _auto_sub_rank_results(scoring["results"])
    scoring["winner"] = scoring["results"][0]
    scoring["runner_up"] = scoring["results"][1] if len(scoring["results"]) >= 2 else None
    scoring["confidence"] = _auto_sub_scoring_confidence(scoring["results"])
    scoring["score_mode"] = f"{channel_name}_fallback"
    scoring["scored_candidates"] = candidates
    return scoring


def _score_auto_sub_combined_candidates(
    candidates: list[dict[str, Any]],
    *,
    crossover_hz: int,
    low_guard_reference_delay_ms: float | None = None,
) -> dict[str, Any]:
    """Score AutoSub candidates with L/R data when available, fallback to one side."""
    both_valid = [
        result for result in candidates
        if _auto_sub_has_points(result, "points_left") and _auto_sub_has_points(result, "points_right")
    ]
    if len(both_valid) >= 2:
        valid_left = []
        valid_right = []
        scan_by_delay = {}
        for result in both_valid:
            delay_key = _auto_sub_delay_key(result)
            scan_by_delay[delay_key] = result.get("scan", "coarse")
            left_result = dict(result)
            left_result["points"] = result["points_left"]
            valid_left.append(left_result)
            right_result = dict(result)
            right_result["points"] = result["points_right"]
            valid_right.append(right_result)

        left_scoring = score_sub_alignment_candidates(
            valid_left,
            crossover_hz=crossover_hz,
            low_guard_reference_delay_ms=low_guard_reference_delay_ms,
        )
        right_scoring = score_sub_alignment_candidates(
            valid_right,
            crossover_hz=crossover_hz,
            low_guard_reference_delay_ms=low_guard_reference_delay_ms,
        )
        left_by_delay = {_auto_sub_delay_key(result): result for result in left_scoring["results"]}
        right_by_delay = {_auto_sub_delay_key(result): result for result in right_scoring["results"]}

        combined_results = []
        for result in both_valid:
            delay_key = _auto_sub_delay_key(result)
            left_result = left_by_delay.get(delay_key)
            right_result = right_by_delay.get(delay_key)
            if not left_result or not right_result:
                continue
            score_left = float(left_result.get("score", 0.0) or 0.0)
            score_right = float(right_result.get("score", 0.0) or 0.0)
            combined_score = 0.6 * min(score_left, score_right) + 0.4 * ((score_left + score_right) / 2.0)
            low_guard_loss = max(
                float(left_result.get("low_guard_loss_db", 0.0) or 0.0),
                float(right_result.get("low_guard_loss_db", 0.0) or 0.0),
            )
            low_guard_penalty = 0.6 * max(
                float(left_result.get("low_guard_penalty", 0.0) or 0.0),
                float(right_result.get("low_guard_penalty", 0.0) or 0.0),
            ) + 0.4 * (
                (
                    float(left_result.get("low_guard_penalty", 0.0) or 0.0)
                    + float(right_result.get("low_guard_penalty", 0.0) or 0.0)
                ) / 2.0
            )
            combined_results.append({
                "delay_ms": result["delay_ms"],
                "name": result.get("name", str(result["delay_ms"])),
                "score": round(combined_score, 4),
                "score_pct": round(combined_score * 100.0, 1),
                "xo_score": round((float(left_result.get("xo_score", 0.0) or 0.0) + float(right_result.get("xo_score", 0.0) or 0.0)) / 2.0, 4),
                "timing_band_score": round((float(left_result.get("timing_band_score", 0.0) or 0.0) + float(right_result.get("timing_band_score", 0.0) or 0.0)) / 2.0, 4),
                "low_guard_loss_db": round(low_guard_loss, 2),
                "low_guard_penalty": round(low_guard_penalty, 4),
                "final_score": round(combined_score, 4),
                "low_guard_loss_L_db": left_result.get("low_guard_loss_db"),
                "low_guard_loss_R_db": right_result.get("low_guard_loss_db"),
                "low_guard_penalty_L": left_result.get("low_guard_penalty"),
                "low_guard_penalty_R": right_result.get("low_guard_penalty"),
                "score_L": round(score_left, 4),
                "score_L_pct": round(score_left * 100.0, 1),
                "score_R": round(score_right, 4),
                "score_R_pct": round(score_right * 100.0, 1),
                "scan": scan_by_delay.get(delay_key, "coarse"),
                "score_source": "lr_combined",
            })

        if not combined_results:
            raise ValueError("No matching L/R AutoSub scoring results")

        combined_results.sort(key=lambda r: r["score"], reverse=True)
        _auto_sub_rank_results(combined_results)
        return {
            "winner": combined_results[0],
            "runner_up": combined_results[1] if len(combined_results) >= 2 else None,
            "results": combined_results,
            "confidence": _auto_sub_scoring_confidence(combined_results),
            "crossover_hz": crossover_hz,
            "score_mode": "lr_combined",
            "scored_candidates": both_valid,
        }

    left_valid = []
    right_valid = []
    for result in candidates:
        if _auto_sub_has_points(result, "points_left"):
            left_result = dict(result)
            left_result["points"] = result["points_left"]
            left_valid.append(left_result)
        if _auto_sub_has_points(result, "points_right"):
            right_result = dict(result)
            right_result["points"] = result["points_right"]
            right_valid.append(right_result)

    if left_valid and len(left_valid) >= len(right_valid):
        return _auto_sub_score_single_channel_fallback(
            left_valid,
            crossover_hz=crossover_hz,
            channel_name="left",
            low_guard_reference_delay_ms=low_guard_reference_delay_ms,
        )
    if right_valid:
        return _auto_sub_score_single_channel_fallback(
            right_valid,
            crossover_hz=crossover_hz,
            channel_name="right",
            low_guard_reference_delay_ms=low_guard_reference_delay_ms,
        )
    raise ValueError("No valid AutoSub sweep results to score")


def _score_auto_sub_matrix_candidates(
    candidates: list[dict[str, Any]],
    *,
    crossover_hz: int,
    original_sub1_alignment_ms: float | None = None,
    original_sub2_alignment_ms: float | None = None,
) -> dict[str, Any]:
    """Score measured 2.2 matrix candidates by Sub1/Sub2 alignment pair."""
    indexed = [
        (idx, result) for idx, result in enumerate(candidates)
        if _auto_sub_has_points(result, "points_left") or _auto_sub_has_points(result, "points_right")
    ]
    if not indexed:
        raise ValueError("No valid AutoSub 2.2 matrix sweep results to score")

    def _low_guard_p20(points: list[list[float]]) -> float:
        low_guard_min_hz = float(crossover_hz) * 0.35
        low_guard_max_hz = float(crossover_hz) * 0.75
        band = [float(point[1]) for point in points if low_guard_min_hz <= float(point[0]) < low_guard_max_hz]
        if not band:
            return float("-inf")
        band.sort()
        p20_index = min(len(band) - 1, max(0, int(round((len(band) - 1) * 0.20))))
        return band[p20_index]

    def _incumbent_index(rows: list[tuple[int, dict[str, Any]]]) -> int | None:
        if original_sub1_alignment_ms is None or original_sub2_alignment_ms is None:
            return None
        for idx, result in rows:
            if _is_incumbent_pair(result):
                return idx
        return None

    def _is_incumbent_pair(result: dict[str, Any]) -> bool:
        if original_sub1_alignment_ms is None or original_sub2_alignment_ms is None:
            return False
        original_sub1 = _auto_sub_clamped_delay(float(original_sub1_alignment_ms))
        original_sub2 = _auto_sub_clamped_delay(float(original_sub2_alignment_ms))
        sub1_alignment = _auto_sub_clamped_delay(float(result.get("sub1_alignment_ms", 0.0) or 0.0))
        sub2_alignment = _auto_sub_clamped_delay(float(result.get("sub2_alignment_ms", 0.0) or 0.0))
        return abs(sub1_alignment - original_sub1) <= 0.05 and abs(sub2_alignment - original_sub2) <= 0.05

    def _reference_index(rows: list[tuple[int, dict[str, Any]]], points_key: str) -> tuple[int | None, str]:
        incumbent_idx = _incumbent_index(rows)
        if incumbent_idx is not None:
            return incumbent_idx, "incumbent"
        valid = [
            (idx, _low_guard_p20(result.get(points_key) or []))
            for idx, result in rows
            if _auto_sub_has_points(result, points_key)
        ]
        if not valid:
            return None, "matrix_best_low_guard"
        return max(valid, key=lambda item: item[1])[0], "matrix_best_low_guard"

    def _copy_for_score(
        result: dict[str, Any],
        idx: int,
        points_key: str,
        reference_idx: int | None,
        reference_label: str,
    ) -> dict[str, Any]:
        candidate = dict(result)
        candidate["delay_ms"] = float(idx)
        candidate["name"] = result.get("name") or _auto_sub_22_name(
            float(result.get("sub1_alignment_ms", 0.0) or 0.0),
            float(result.get("sub2_alignment_ms", 0.0) or 0.0),
        )
        candidate["points"] = result.get(points_key) or []
        if reference_idx is not None and idx == reference_idx:
            candidate["low_guard_reference"] = True
            candidate["low_guard_reference_label"] = reference_label
        return candidate

    def _combined_low_guard_reference(left_result: dict[str, Any], right_result: dict[str, Any]) -> str:
        left_ref = str(left_result.get("low_guard_reference") or "")
        right_ref = str(right_result.get("low_guard_reference") or "")
        if left_ref == right_ref:
            return left_ref
        if {left_ref, right_ref} <= {"incumbent", "matrix_best_low_guard"}:
            return "mixed"
        return f"L:{left_ref} / R:{right_ref}"

    def _finalize_matrix_scoring(results: list[dict[str, Any]], *, score_mode: str, scored_candidates: list[dict[str, Any]]) -> dict[str, Any]:
        _auto_sub_rank_results(results)
        incumbent_winner = next((result for result in results if bool(result.get("incumbent_pair"))), None)
        matrix_winner = next((result for result in results if not bool(result.get("incumbent_pair"))), None)
        if matrix_winner is None:
            matrix_winner = results[0]

        accepted_winner = matrix_winner
        incumbent_accepted = False
        reject_reason = "matrix_better"
        if incumbent_winner is not None:
            incumbent_score = _auto_sub_score_value(incumbent_winner)
            matrix_score = _auto_sub_score_value(matrix_winner)
            if matrix_score <= incumbent_score:
                accepted_winner = incumbent_winner
                incumbent_accepted = True
                reject_reason = "incumbent_better"

        return {
            "winner": accepted_winner,
            "runner_up": results[1] if len(results) >= 2 else None,
            "results": results,
            "confidence": _auto_sub_scoring_confidence(results),
            "crossover_hz": crossover_hz,
            "score_mode": score_mode,
            "scored_candidates": scored_candidates,
            "matrix_winner": matrix_winner,
            "incumbent_winner": incumbent_winner,
            "incumbent_score": round(_auto_sub_score_value(incumbent_winner), 4) if incumbent_winner else None,
            "accepted_winner": accepted_winner,
            "incumbent_accepted": incumbent_accepted,
            "reject_reason": reject_reason,
        }

    both_valid = [
        (idx, result) for idx, result in indexed
        if _auto_sub_has_points(result, "points_left") and _auto_sub_has_points(result, "points_right")
    ]
    if len(both_valid) >= 2:
        left_reference_idx, left_reference_label = _reference_index(both_valid, "points_left")
        right_reference_idx, right_reference_label = _reference_index(both_valid, "points_right")
        left_scoring = score_sub_alignment_candidates(
            [_copy_for_score(result, idx, "points_left", left_reference_idx, left_reference_label) for idx, result in both_valid],
            crossover_hz=crossover_hz,
        )
        right_scoring = score_sub_alignment_candidates(
            [_copy_for_score(result, idx, "points_right", right_reference_idx, right_reference_label) for idx, result in both_valid],
            crossover_hz=crossover_hz,
        )
        left_by_idx = {int(round(float(result.get("delay_ms", 0.0)))): result for result in left_scoring["results"]}
        right_by_idx = {int(round(float(result.get("delay_ms", 0.0)))): result for result in right_scoring["results"]}
        combined_results = []
        for idx, result in both_valid:
            left_result = left_by_idx.get(idx)
            right_result = right_by_idx.get(idx)
            if not left_result or not right_result:
                continue
            score_left = float(left_result.get("score", 0.0) or 0.0)
            score_right = float(right_result.get("score", 0.0) or 0.0)
            combined_score = 0.6 * min(score_left, score_right) + 0.4 * ((score_left + score_right) / 2.0)
            low_guard_loss = max(
                float(left_result.get("low_guard_loss_db", 0.0) or 0.0),
                float(right_result.get("low_guard_loss_db", 0.0) or 0.0),
            )
            low_guard_penalty = 0.6 * max(
                float(left_result.get("low_guard_penalty", 0.0) or 0.0),
                float(right_result.get("low_guard_penalty", 0.0) or 0.0),
            ) + 0.4 * (
                (
                    float(left_result.get("low_guard_penalty", 0.0) or 0.0)
                    + float(right_result.get("low_guard_penalty", 0.0) or 0.0)
                ) / 2.0
            )
            sub1_alignment = _auto_sub_clamped_delay(float(result.get("sub1_alignment_ms", 0.0) or 0.0))
            sub2_alignment = _auto_sub_clamped_delay(float(result.get("sub2_alignment_ms", 0.0) or 0.0))
            combined_results.append({
                "delay_ms": sub1_alignment,
                "sub1_alignment_ms": sub1_alignment,
                "sub2_alignment_ms": sub2_alignment,
                "incumbent_pair": _is_incumbent_pair(result),
                "name": result.get("name") or _auto_sub_22_name(sub1_alignment, sub2_alignment),
                "score": round(combined_score, 4),
                "score_pct": round(combined_score * 100.0, 1),
                "xo_score": round((float(left_result.get("xo_score", 0.0) or 0.0) + float(right_result.get("xo_score", 0.0) or 0.0)) / 2.0, 4),
                "timing_band_score": round((float(left_result.get("timing_band_score", 0.0) or 0.0) + float(right_result.get("timing_band_score", 0.0) or 0.0)) / 2.0, 4),
                "low_guard_loss_db": round(low_guard_loss, 2),
                "low_guard_penalty": round(low_guard_penalty, 4),
                "final_score": round(combined_score, 4),
                "low_guard_loss_L_db": left_result.get("low_guard_loss_db"),
                "low_guard_loss_R_db": right_result.get("low_guard_loss_db"),
                "low_guard_penalty_L": left_result.get("low_guard_penalty"),
                "low_guard_penalty_R": right_result.get("low_guard_penalty"),
                "low_guard_reference": _combined_low_guard_reference(left_result, right_result),
                "low_guard_reference_L": left_result.get("low_guard_reference"),
                "low_guard_reference_R": right_result.get("low_guard_reference"),
                "score_L": round(score_left, 4),
                "score_L_pct": round(score_left * 100.0, 1),
                "score_R": round(score_right, 4),
                "score_R_pct": round(score_right * 100.0, 1),
                "scan": result.get("scan", "combined_matrix"),
                "score_source": "lr_combined",
            })
        if not combined_results:
            raise ValueError("No matching L/R AutoSub 2.2 matrix scoring results")
        combined_results.sort(key=lambda r: r["score"], reverse=True)
        return _finalize_matrix_scoring(
            combined_results,
            score_mode="lr_combined_matrix",
            scored_candidates=[result for _, result in both_valid],
        )

    fallback_key = "points_left"
    channel_name = "left"
    fallback = [(idx, result) for idx, result in indexed if _auto_sub_has_points(result, fallback_key)]
    right_fallback = [(idx, result) for idx, result in indexed if _auto_sub_has_points(result, "points_right")]
    if len(right_fallback) > len(fallback):
        fallback_key = "points_right"
        channel_name = "right"
        fallback = right_fallback
    if not fallback:
        raise ValueError("No valid AutoSub 2.2 matrix sweep results to score")

    fallback_reference_idx, fallback_reference_label = _reference_index(fallback, fallback_key)
    single_scoring = score_sub_alignment_candidates(
        [_copy_for_score(result, idx, fallback_key, fallback_reference_idx, fallback_reference_label) for idx, result in fallback],
        crossover_hz=crossover_hz,
    )
    by_idx = {idx: result for idx, result in fallback}
    matrix_results = []
    for scored in single_scoring["results"]:
        idx = int(round(float(scored.get("delay_ms", 0.0))))
        measured = by_idx.get(idx) or {}
        sub1_alignment = _auto_sub_clamped_delay(float(measured.get("sub1_alignment_ms", 0.0) or 0.0))
        sub2_alignment = _auto_sub_clamped_delay(float(measured.get("sub2_alignment_ms", 0.0) or 0.0))
        score = round(float(scored.get("score", 0.0) or 0.0), 4)
        score_pct = round(score * 100.0, 1)
        matrix_result = {
            "delay_ms": sub1_alignment,
            "sub1_alignment_ms": sub1_alignment,
            "sub2_alignment_ms": sub2_alignment,
            "incumbent_pair": _is_incumbent_pair(measured),
            "name": measured.get("name") or _auto_sub_22_name(sub1_alignment, sub2_alignment),
            "score": score,
            "score_pct": score_pct,
            "xo_score": scored.get("xo_score"),
            "timing_band_score": scored.get("timing_band_score"),
            "low_guard_loss_db": scored.get("low_guard_loss_db"),
            "low_guard_penalty": scored.get("low_guard_penalty"),
            "low_guard_reference": scored.get("low_guard_reference"),
            "final_score": scored.get("final_score", score),
            "scan": measured.get("scan", "combined_matrix"),
            "score_source": f"{channel_name}_fallback",
        }
        if channel_name == "left":
            matrix_result.update({"score_L": score, "score_L_pct": score_pct, "score_R": None, "score_R_pct": None})
        else:
            matrix_result.update({"score_L": None, "score_L_pct": None, "score_R": score, "score_R_pct": score_pct})
        matrix_results.append(matrix_result)
    return _finalize_matrix_scoring(
        matrix_results,
        score_mode=f"{channel_name}_fallback_matrix",
        scored_candidates=[result for _, result in fallback],
    )


def _auto_sub_candidate_ledger(
    candidates: list[dict[str, Any]],
    scoring: dict[str, Any],
    *,
    mode: str,
    phase: str,
    channel: str | None = None,
    roles: dict[str, dict[str, Any] | None] | None = None,
    decision_pool: list[dict[str, Any]] | None = None,
    requested_incumbent: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Describe scorer decisions without participating in them.

    This deliberately runs only after a scorer has returned.  It reconstructs
    inclusion from the scorer output and never supplies data back to scoring.
    """
    score_mode = str(scoring.get("score_mode") or "")
    matrix = phase == "matrix"

    def key(result: dict[str, Any]) -> tuple[float, ...]:
        if matrix:
            return (
                round(float(result.get("sub1_alignment_ms", 0.0) or 0.0), 2),
                round(float(result.get("sub2_alignment_ms", 0.0) or 0.0), 2),
            )
        return (round(float(result.get("delay_ms", 0.0) or 0.0), 2),)

    scored_by_key = {key(result): result for result in scoring.get("results", [])}
    complete_available = sum(
        1 for result in (decision_pool if decision_pool is not None else candidates)
        if _auto_sub_has_points(result, "points_left") and _auto_sub_has_points(result, "points_right")
    ) >= 2
    majority_channel = "left" if score_mode.startswith("left_") else "right" if score_mode.startswith("right_") else None
    role_keys = {
        name: key(result) for name, result in (roles or {}).items() if isinstance(result, dict)
    }
    if requested_incumbent is not None:
        role_keys["incumbent"] = key(requested_incumbent)
    ledger: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_key = key(candidate)
        left_count = len(candidate.get("points_left") or (candidate.get("points") if channel == "left" else []) or [])
        right_count = len(candidate.get("points_right") or (candidate.get("points") if channel == "right" else []) or [])
        if channel == "left":
            eligible = left_count >= 3
        elif channel == "right":
            eligible = right_count >= 3
        else:
            eligible = left_count >= 3 or right_count >= 3
        scored = scored_by_key.get(candidate_key)
        included = scored is not None
        reason = None
        if not eligible:
            if channel == "left":
                reason = "left_insufficient_points"
            elif channel == "right":
                reason = "right_insufficient_points"
            elif left_count < 3 and right_count < 3:
                reason = "both_insufficient_points"
            elif left_count < 3:
                reason = "left_insufficient_points"
            else:
                reason = "right_insufficient_points"
        elif not included:
            left_ok = left_count >= 3
            right_ok = right_count >= 3
            if complete_available and not (left_ok and right_ok):
                reason = "single_side_excluded_because_complete_candidates_available"
            elif majority_channel and not ((majority_channel == "left" and left_ok) or (majority_channel == "right" and right_ok)):
                reason = "excluded_by_majority_side_fallback"
            else:
                reason = "delay_key_merge_failed"
        row: dict[str, Any] = {
            "mode": mode,
            "phase": phase,
            "requested_delay_ms": candidate.get("delay_ms"),
            "requested_sub1_alignment_ms": candidate.get("sub1_alignment_ms"),
            "requested_sub2_alignment_ms": candidate.get("sub2_alignment_ms"),
            "delay_ms": candidate.get("delay_ms"),
            "sub1_alignment_ms": candidate.get("sub1_alignment_ms"),
            "sub2_alignment_ms": candidate.get("sub2_alignment_ms"),
            "status_left": candidate.get("status") if channel == "left" else candidate.get("status_left"),
            "status_right": candidate.get("status") if channel == "right" else candidate.get("status_right"),
            "points_left": left_count,
            "points_right": right_count,
            "eligible_for_scoring": eligible,
            "included_in_scoring": included,
            "exclusion_reason": reason,
            "score": scored.get("score") if scored else None,
            "final_score": scored.get("final_score", scored.get("score")) if scored else None,
            "score_pct": scored.get("score_pct") if scored else None,
            "score_left": scored.get("score_L") if scored else None,
            "score_right": scored.get("score_R") if scored else None,
            "roles": sorted(name for name, role_key in role_keys.items() if role_key == candidate_key),
        }
        ledger.append(row)
        logger.info("AUTOSUB_CANDIDATE %s", json.dumps(row, sort_keys=True, separators=(",", ":")))
    return ledger


@app.post("/api/measurements/auto-sub-optimize/start")
async def start_auto_sub_optimize(
    input_id: str = Form(...),
    channel: str = Form("left"),
    mic_input_channel: str = Form("1"),
    reference_input_channel: str = Form(""),
    calibration_ref: str = Form(""),
    target_curve_snapshot: str = Form(""),
    calibration_file: UploadFile | None = File(None),
):
    global measurement_store, subwoofer_runtime, _auto_sub_lock
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    if not _auto_sub_lock:
        _auto_sub_lock = asyncio.Lock()

    from samplerate import _load_audio_output_mode, set_audio_output_mode

    # Reject if any measurement is already running
    if measurement_store.has_active_measurement_job():
        raise HTTPException(status_code=409, detail="Another measurement is already running")

    # Clean up stale cancelling jobs before lock acquisition
    _cleanup_stale_autosub_cancelling_jobs()

    # Acquire lock before modifying AutoSub state
    try:
        await asyncio.wait_for(_auto_sub_lock.acquire(), timeout=0.5)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=423, detail="Auto Sub Optimize is already in progress")

    try:
        mode_state = _load_audio_output_mode()
        output_mode = mode_state.get("mode")
        if output_mode not in OUTPUT_MODE_SUBWOOFER_MODES:
            raise HTTPException(status_code=400, detail="Auto Sub Optimize requires 2.1 or 2.2 Subwoofer output mode")

        if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
            sub1 = _auto_sub_22_sub(mode_state, "sub1")
            sub2 = _auto_sub_22_sub(mode_state, "sub2")
            fc = int(mode_state.get("crossover_frequency_hz", 80))
            current_alignment = float(sub1.get("alignment_ms", 0.0))
            current_sub2_alignment = float(sub2.get("alignment_ms", 0.0))
            original_polarity = str(sub1.get("polarity", "normal"))
            original_level = float(sub1.get("level_db", 0.0))
            original_highpass = bool(mode_state.get("main_highpass_enabled", True))
        else:
            sub = mode_state.get("subwoofer") or {}
            fc = int(sub.get("crossover_frequency_hz", 80))
            current_alignment = float(sub.get("sub_alignment_ms", 0.0))
            current_sub2_alignment = 0.0
            original_polarity = str(sub.get("sub_polarity", "normal"))
            original_level = float(sub.get("sub_level_db", 0.0))
            original_highpass = bool(sub.get("main_highpass_enabled", True))

        # Compute scan range
        step_ms = _auto_sub_step_ms(fc)
        coarse_steps = 4
        scan_delays: list[float] = []
        for s in range(-coarse_steps, coarse_steps + 1):
            delay = _auto_sub_clamped_delay(current_alignment + s * step_ms)
            if not scan_delays or abs(delay - scan_delays[-1]) > 0.05:
                scan_delays.append(delay)

        # Snapshot original config for rollback
        original_config_snapshot = _auto_sub_snapshot_copy(mode_state)
        target_curve, target_curve_error = _validate_auto_sub_target_curve_snapshot(target_curve_snapshot)

        job_id = f"auto-sub-{uuid4().hex[:12]}"
        job: dict[str, Any] = {
            "id": job_id,
            "status": "preparing",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "message": f"Auto Sub Optimize: {len(scan_delays)} candidates @ {fc} Hz",
            "result": None,
            "error": None,
            "mode": output_mode,
            "crossover_hz": fc,
            "scan_delays": scan_delays,
            "step_ms": step_ms,
            "original_alignment_ms": current_alignment,
            "original_sub1_alignment_ms": current_alignment,
            "original_sub2_alignment_ms": current_sub2_alignment,
            "original_config_snapshot": original_config_snapshot,
            "target_curve": target_curve,
            "auto_gain": {
                "available": False,
                "reason": target_curve_error or "Vertical Main/Target level reference has not passed its mandatory gate",
            },
            "current_sweep_id": "",
            "cancel_requested": False,
            "cancelled_at": None,
            "fine_scan": {
                "enabled": False,
                "triggered": False,
                "status": "pending",
                "candidates": [],
            },
        }
        _AUTO_SUB_JOBS[job_id] = job
        logger.info(
            "AUTOSUB job=%s start mode=%s fc=%sHz candidates=%s target_curve=%s auto_gain=%s",
            job_id, output_mode, fc, len(scan_delays),
            json.dumps(target_curve, sort_keys=True, separators=(",", ":")) if target_curve else "unavailable",
            json.dumps(job["auto_gain"], sort_keys=True, separators=(",", ":")),
        )

        calibration_bytes = None
        calibration_filename = None
        if calibration_file is not None:
            calibration_filename = calibration_file.filename or "calibration.txt"
            content_type = str(calibration_file.content_type or "").lower()
            if "text" not in content_type and "plain" not in content_type and content_type not in ("", "application/octet-stream"):
                raise HTTPException(status_code=400, detail="Calibration file must be a text file")
            raw_bytes = await calibration_file.read()
            if len(raw_bytes) > _AUTO_SUB_MAX_CALIBRATION_BYTES:
                raise HTTPException(status_code=400, detail=f"Calibration file too large (max {_AUTO_SUB_MAX_CALIBRATION_BYTES // (1024*1024)} MiB)")
            calibration_bytes = raw_bytes

        if output_mode == OUTPUT_MODE_SUBWOOFER_22_STEREO:
            fine_step_ms = step_ms / 4.0
            right_scan_delays: list[float] = []
            for s in range(-coarse_steps, coarse_steps + 1):
                delay = _auto_sub_clamped_delay(current_sub2_alignment + s * step_ms)
                if not right_scan_delays or abs(delay - right_scan_delays[-1]) > 0.05:
                    right_scan_delays.append(delay)
            job["message"] = (
                f"Auto Sub Optimize 2.2 Stereo Bass: Left Sub {len(scan_delays)} coarse, "
                f"Left fine up to 6, Right Sub {len(right_scan_delays)} coarse, Right fine up to 6 @ {fc} Hz"
            )
            job["scan_delays"] = {"left_sub": scan_delays, "right_sub": right_scan_delays}
            job["fine_scan"] = {
                "enabled": True,
                "triggered": False,
                "status": "pending",
                "reason": "2.2 Stereo Bass optimizes Left and Right Sub separately with per-side fine scans",
                "fine_step_ms": fine_step_ms,
                "left": {"status": "pending", "candidates": []},
                "right": {"status": "pending", "candidates": []},
            }
            asyncio.create_task(
                _run_auto_sub_22_stereo_optimize(
                    job_id=job_id,
                    input_id=input_id,
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    left_scan_delays=scan_delays,
                    right_scan_delays=right_scan_delays,
                    fc=fc,
                    original_config_snapshot=original_config_snapshot,
                )
            )
        elif output_mode == OUTPUT_MODE_SUBWOOFER_22:
            fine_step_ms = step_ms / 4.0
            sub2_scan_delays: list[float] = []
            for s in range(-coarse_steps, coarse_steps + 1):
                delay = _auto_sub_clamped_delay(current_sub2_alignment + s * step_ms)
                if not sub2_scan_delays or abs(delay - sub2_scan_delays[-1]) > 0.05:
                    sub2_scan_delays.append(delay)
            job["message"] = (
                f"Auto Sub Optimize 2.2: Sub 1 {len(scan_delays)} coarse, "
                f"Sub 2 {len(sub2_scan_delays)} coarse, 3x3 matrix @ {fc} Hz"
            )
            job["scan_delays"] = {"sub1": scan_delays, "sub2": sub2_scan_delays}
            job["combined_matrix"] = {"status": "pending", "fine_step_ms": fine_step_ms, "candidates": []}
            asyncio.create_task(
                _run_auto_sub_22_optimize(
                    job_id=job_id,
                    input_id=input_id,
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    sub1_scan_delays=scan_delays,
                    sub2_scan_delays=sub2_scan_delays,
                    fc=fc,
                    original_config_snapshot=original_config_snapshot,
                    fine_step_ms=fine_step_ms,
                )
            )
        else:
            asyncio.create_task(
                _run_auto_sub_optimize(
                    job_id=job_id,
                    input_id=input_id,
                    channel=channel,
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    scan_delays=scan_delays,
                    fc=fc,
                    current_alignment=current_alignment,
                    original_polarity=original_polarity,
                    original_level=original_level,
                    original_highpass=original_highpass,
                    original_config_snapshot=original_config_snapshot,
                )
            )
        return {"status": "ok", "job": job}
    except HTTPException:
        _auto_sub_lock.release()
        raise
    except Exception:
        _auto_sub_lock.release()
        raise


@app.get("/api/measurements/auto-sub-optimize/jobs/{job_id}")
async def get_auto_sub_optimize_job(job_id: str):
    job = _AUTO_SUB_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Auto Sub Optimize job not found")
    return {"status": "ok", "job": job}


@app.post("/api/measurements/auto-sub-optimize/jobs/{job_id}/cancel")
async def cancel_auto_sub_optimize_job(job_id: str):
    global measurement_store
    job = _AUTO_SUB_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Auto Sub Optimize job not found")
    if str(job.get("status") or "").lower() in {"completed", "failed", "cancelled"}:
        return {"status": "ok", "job": job}

    job["cancel_requested"] = True
    job["cancelled_at"] = datetime.now(timezone.utc).isoformat()
    job["status"] = "cancelling"
    job["message"] = "Auto Sub Optimize cancelling..."
    job["error"] = None
    logger.info("AUTOSUB job=%s cancel requested", job_id)

    current_sweep_id = job.get("current_sweep_id")
    if current_sweep_id and measurement_store:
        try:
            measurement_store.cancel_job(str(current_sweep_id))
        except KeyError:
            pass
        except Exception as exc:
            logger.warning("Auto-sub: failed to cancel current sweep %s: %s", current_sweep_id, exc)

    return {"status": "ok", "job": job}


_AUTO_SUB_TIMING_MARKS = [
    "config_set",
    "config_verify",
    "pre_arm",
    "sweep_start",
    "sweep_poll_done",
    "release_start",
    "release_done",
]

_AUTO_SUB_STAGE_PEAK_LIMIT_DBFS = -1.0
_AUTO_SUB_STAGE_PEAK_MISMATCH_DB = 1.0


class AutoSubPeakSafetyError(RuntimeError):
    """Abort the complete AutoSub run after a Stage1 peak safety failure."""


def _auto_sub_stage_peak_prediction(
    *, sweep_profile: dict[str, Any], sample_rate: int, channel: str,
    config: SubwooferRuntimeConfig,
) -> dict[str, Any]:
    """Run the known measurement PCM through the native Stage1 DSP topology."""
    rate = int(sample_rate)
    duration = float(sweep_profile["sweep_seconds"])
    count = max(2048, int(round(rate * duration)))
    t = np.arange(count, dtype=np.float64) / rate
    start_hz = float(sweep_profile["sweep_start_hz"])
    end_hz = float(sweep_profile["sweep_end_hz"])
    log_ratio = math.log(end_hz / start_hz)
    phase = 2.0 * math.pi * start_hz * duration / log_ratio * (np.exp(t * log_ratio / duration) - 1.0)
    sweep = np.sin(phase)
    fade_len = min(count // 8, max(64, int(round(rate * 0.01))))
    if fade_len > 1:
        sweep[:fade_len] *= np.linspace(0.0, 1.0, fade_len)
        sweep[-fade_len:] *= np.linspace(1.0, 0.0, fade_len)
    sweep *= 0.8 / max(float(np.max(np.abs(sweep))), 1e-12)
    zeros = np.zeros_like(sweep)
    left = sweep if channel in ("left", "stereo") else zeros
    right = sweep if channel in ("right", "stereo") else zeros

    def coefficients(kind: str) -> tuple[list[float], list[float]]:
        w0 = 2.0 * math.pi * config.crossover_frequency_hz / rate
        cos_w0, sin_w0 = math.cos(w0), math.sin(w0)
        alpha = sin_w0 / (2.0 * math.sqrt(0.5))
        a0 = 1.0 + alpha
        if kind == "low":
            b = [(1.0 - cos_w0) * 0.5 / a0, (1.0 - cos_w0) / a0,
                 (1.0 - cos_w0) * 0.5 / a0]
        else:
            b = [(1.0 + cos_w0) * 0.5 / a0, -(1.0 + cos_w0) / a0,
                 (1.0 + cos_w0) * 0.5 / a0]
        return b, [1.0, -2.0 * cos_w0 / a0, (1.0 - alpha) / a0]

    def lr24(signal: np.ndarray, kind: str, enabled: bool = True) -> np.ndarray:
        if not enabled:
            return signal.copy()
        b, a = coefficients(kind)
        def one_stage(values: np.ndarray) -> np.ndarray:
            output = np.empty_like(values)
            z1 = 0.0
            z2 = 0.0
            for index, value in enumerate(values):
                filtered = b[0] * value + z1
                z1 = b[1] * value - a[1] * filtered + z2
                z2 = b[2] * value - a[2] * filtered
                output[index] = filtered
            return output
        return one_stage(one_stage(signal))

    def delay(signal: np.ndarray, delay_ms: float) -> np.ndarray:
        samples = int(float(delay_ms) * rate / 1000.0 + 0.5)
        if samples <= 0:
            return signal
        return np.concatenate((np.zeros(samples), signal))[:signal.size]

    main_l = delay(lr24(left, "high", config.main_highpass_enabled), config.derived_main_delay_ms)
    main_r = delay(lr24(right, "high", config.main_highpass_enabled), config.derived_main_delay_ms)
    if config.bass_routing == "stereo":
        sub1_source, sub2_source = left, right
    else:
        sub1_source = sub2_source = (left + right) * 0.5
    sub1 = delay(lr24(sub1_source, "low"), config.derived_sub1_delay_ms)
    sub2 = delay(lr24(sub2_source, "low"), config.derived_sub2_delay_ms)
    sub1 *= (-1.0 if config.sub_polarity == "invert" else 1.0) * 10.0 ** (config.sub_level_db / 20.0)
    sub2 *= (-1.0 if config.sub2_polarity == "invert" else 1.0) * 10.0 ** (config.sub2_level_db / 20.0)
    peaks = {
        f"output_{index + 1}": float(np.max(np.abs(signal))) if signal.size else 0.0
        for index, signal in enumerate((main_l, main_r, sub1, sub2))
    }
    peak_dbfs = {key: round(20.0 * math.log10(max(value, 1e-12)), 3) for key, value in peaks.items()}
    return {
        "linear": peaks,
        "dbfs": peak_dbfs,
        "maximum_dbfs": max(peak_dbfs.values()),
        "limit_dbfs": _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS,
        "safe": max(peak_dbfs.values()) <= _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS,
    }


def _auto_sub_stage_peak_comparison(
    predicted: dict[str, Any], measured_linear: dict[str, float],
) -> dict[str, Any]:
    measured_dbfs = {
        key: round(20.0 * math.log10(max(float(value), 1e-12)), 3)
        for key, value in measured_linear.items()
    }
    differences = {
        key: round(measured_dbfs[key] - float(predicted["dbfs"][key]), 3)
        for key in measured_dbfs
        if max(measured_dbfs[key], float(predicted["dbfs"][key])) > -90.0
    }
    relevant = any(abs(value) > _AUTO_SUB_STAGE_PEAK_MISMATCH_DB for value in differences.values())
    return {
        "predicted": predicted,
        "measured": {"linear": measured_linear, "dbfs": measured_dbfs},
        "difference_db": differences,
        "tolerance_db": _AUTO_SUB_STAGE_PEAK_MISMATCH_DB,
        "relevant_mismatch": relevant,
        "measured_safe": max(measured_dbfs.values()) <= _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS,
    }


def _auto_sub_timing_durations(marks: dict[str, float]) -> dict[str, float]:
    durations: dict[str, float] = {}
    prev_key = "start"
    for key in _AUTO_SUB_TIMING_MARKS:
        if key in marks and prev_key in marks:
            durations[f"{prev_key}_to_{key}_ms"] = round((marks[key] - marks[prev_key]) * 1000, 1)
        if key in marks:
            prev_key = key
    if "start" in marks and prev_key in marks:
        durations["total_ms"] = round((marks[prev_key] - marks["start"]) * 1000, 1)
    return durations


def _append_auto_sub_sweep_timing(
    job: dict[str, Any],
    *,
    delay_ms: float,
    channel: str,
    candidate_index: int,
    candidate_current: int | None,
    stage: str,
    status: str,
    marks: dict[str, float],
) -> None:
    job.setdefault("_sweep_timings", []).append({
        "delay_ms": delay_ms,
        "channel": channel,
        "stage": stage,
        "durations": _auto_sub_timing_durations(marks),
        "candidate": candidate_current or candidate_index,
        "sweep_index": candidate_index,
        "status": status,
    })


def _log_auto_sub_timing_summary(job: dict[str, Any]) -> None:
    timing_log = job.get("_sweep_timings", [])
    if not timing_log:
        return

    def _sum_phase(phase: str) -> float:
        return sum((t.get("durations", {}) or {}).get(phase, 0) or 0 for t in timing_log)

    total_config = _sum_phase("start_to_config_set_ms")
    total_verify = _sum_phase("config_set_to_config_verify_ms")
    total_prearm = _sum_phase("config_verify_to_pre_arm_ms")
    total_sweep = _sum_phase("pre_arm_to_sweep_start_ms")
    total_poll = _sum_phase("sweep_start_to_sweep_poll_done_ms")
    total_release = _sum_phase("sweep_poll_done_to_release_start_ms")
    total_cleanup = _sum_phase("release_start_to_release_done_ms")
    total_all = _sum_phase("total_ms")

    logger.info(
        "Auto-sub timing summary: count=%d sweeps total=%.1fs "
        "config=%.1fs verify=%.1fs prearm=%.1fs sweep=%.1fs poll=%.1fs release=%.1fs cleanup=%.1fs "
        "idle=%.1fs",
        len(timing_log), total_all / 1000,
        total_config / 1000, total_verify / 1000, total_prearm / 1000,
        total_sweep / 1000, total_poll / 1000, total_release / 1000, total_cleanup / 1000,
        max(0, (total_all - total_config - total_verify - total_prearm - total_sweep - total_poll - total_release - total_cleanup)) / 1000,
    )

    l_timings = [t for t in timing_log if t.get("channel") == "left"]
    r_timings = [t for t in timing_log if t.get("channel") == "right"]
    if l_timings:
        l_avg = sum((t.get("durations", {}) or {}).get("total_ms", 0) or 0 for t in l_timings) / len(l_timings)
        r_avg = (
            sum((t.get("durations", {}) or {}).get("total_ms", 0) or 0 for t in r_timings) / len(r_timings)
            if r_timings
            else 0
        )
        logger.info("Auto-sub timing: L avg=%.1fms R avg=%.1fms", l_avg, r_avg)


async def _measure_auto_sub_candidate(
    *,
    delay_ms: float,
    job: dict[str, Any],
    candidate_index: int,
    total: int,
    stage: str,
    fc: int,
    input_id: str,
    channel: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    auto_sub_sweep_profile: dict[str, Any],
    auto_sub_rate: int,
    original_level: float,
    original_polarity: str,
    original_highpass: bool,
    measurement_label: str | None = None,
    candidate_current: int | None = None,
    candidate_total: int | None = None,
    measure_channel: str | None = None,
    output_mode: str = OUTPUT_MODE_SUBWOOFER_21,
    original_config_snapshot: dict[str, Any] | None = None,
    sub1_alignment_ms: float | None = None,
    sub2_alignment_ms: float | None = None,
    active_subs: tuple[str, ...] = ("sub1",),
    sub1_polarity: str | None = None,
    sub2_polarity: str | None = None,
    exact_sub_mute: bool = False,
) -> dict[str, Any]:
    """Measure one AutoSub delay candidate with the standard safety checks."""
    from samplerate import _load_audio_output_mode

    _marks = {"start": time.monotonic()}
    _timing_written = False

    def _return_candidate(result: dict[str, Any]) -> dict[str, Any]:
        nonlocal _timing_written
        if not _timing_written:
            _marks.setdefault("release_done", time.monotonic())
            _append_auto_sub_sweep_timing(
                job,
                delay_ms=delay_ms,
                channel=measure_channel or channel,
                candidate_index=candidate_index,
                candidate_current=candidate_current,
                stage=stage,
                status=str(result.get("status") or "unknown"),
                marks=_marks,
            )
            _timing_written = True
        return result

    if _auto_sub_cancel_requested(job):
        return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))

    label = "Fine-Scan" if stage == "fine" else "Coarse scan"
    job["status"] = "running"
    job["message"] = measurement_label or f"{label}: sweep {candidate_index}/{total} @ sub_alignment_ms={delay_ms:.2f} ms"
    job["progress"] = {
        "current": candidate_index,
        "total": total,
        "delay_ms": delay_ms,
        "stage": stage,
        "sweep_current": candidate_index,
        "sweep_total": total,
    }
    if candidate_current is not None and candidate_total is not None:
        job["progress"]["candidate_current"] = candidate_current
        job["progress"]["candidate_total"] = candidate_total
    if measure_channel:
        job["progress"]["channel"] = measure_channel

    config_success = False
    try:
        if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
            snapshot = original_config_snapshot or {}
            sub1_delay = _auto_sub_clamped_delay(sub1_alignment_ms if sub1_alignment_ms is not None else delay_ms)
            sub2_delay = _auto_sub_clamped_delay(sub2_alignment_ms if sub2_alignment_ms is not None else _auto_sub_22_sub(snapshot, "sub2").get("alignment_ms", 0.0))
            sub_config = _auto_sub_22_global_config(snapshot)
            subwoofers_config = _auto_sub_22_candidate_subwoofers(
                snapshot,
                sub1_alignment_ms=sub1_delay,
                sub2_alignment_ms=sub2_delay,
                active_subs=active_subs,
                sub1_polarity=sub1_polarity,
                sub2_polarity=sub2_polarity,
            )
            set_audio_output_mode(output_mode, sub_config, subwoofers_config)
        else:
            sub_config = {
                "crossover_frequency_hz": fc,
                "sub_alignment_ms": delay_ms,
                "sub_level_db": original_level,
                "sub_polarity": original_polarity,
                "main_highpass_enabled": original_highpass,
            }
            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, sub_config)
        if subwoofer_runtime is not None:
            config = SubwooferRuntimeConfig.from_overview(get_audio_output_overview())
            await subwoofer_runtime.sync(config)
        _marks["config_set"] = time.monotonic()
        await asyncio.sleep(0.5)
        if _auto_sub_cancel_requested(job):
            return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
        verify = _load_audio_output_mode()
        if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
            config_success = _auto_sub_22_verify_alignment(verify, sub1_delay, sub2_delay)
        else:
            config_success = float(verify.get("subwoofer", {}).get("sub_alignment_ms", -999)) == delay_ms
        if not config_success:
            await asyncio.sleep(0.15)
            if _auto_sub_cancel_requested(job):
                return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
            verify = _load_audio_output_mode()
            if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
                config_success = _auto_sub_22_verify_alignment(verify, sub1_delay, sub2_delay)
            else:
                config_success = float(verify.get("subwoofer", {}).get("sub_alignment_ms", -999)) == delay_ms
            if not config_success:
                await asyncio.sleep(0.5)
                if _auto_sub_cancel_requested(job):
                    return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
                verify = _load_audio_output_mode()
                if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
                    config_success = _auto_sub_22_verify_alignment(verify, sub1_delay, sub2_delay)
                else:
                    config_success = float(verify.get("subwoofer", {}).get("sub_alignment_ms", -999)) == delay_ms
        _marks["config_verify"] = time.monotonic()
    except Exception as exc:
        logger.warning("Auto-sub: failed to configure delay %.2f ms: %s", delay_ms, exc)

    if not config_success:
        logger.warning("Auto-sub: skipping candidate %.2f ms — config sync failed", delay_ms)
        return _return_candidate({
            "delay_ms": delay_ms,
            "name": str(delay_ms),
            "points": [],
            "sweep_id": "",
            "status": "config_failed",
            "error": "Subwoofer config sync failed",
            "scan": stage,
        })

    try:
        await _sync_subwoofer_runtime_for_measurement_sweep(auto_sub_rate)
        _marks["pre_arm"] = time.monotonic()
        if _auto_sub_cancel_requested(job):
            return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
    except Exception as exc:
        logger.exception("Auto-sub: pre-arm failed for delay %.2f ms", delay_ms)
        return _return_candidate({
            "delay_ms": delay_ms,
            "name": str(delay_ms),
            "points": [],
            "sweep_id": "",
            "status": "pre_arm_failed",
            "error": str(exc),
            "scan": stage,
        })
    stage_peak_prediction = _auto_sub_stage_peak_prediction(
        sweep_profile=auto_sub_sweep_profile,
        sample_rate=auto_sub_rate,
        channel=channel,
        config=SubwooferRuntimeConfig.from_overview(get_audio_output_overview()),
    )
    if exact_sub_mute:
        for key in ("output_3", "output_4"):
            stage_peak_prediction["linear"][key] = 0.0
            stage_peak_prediction["dbfs"][key] = -240.0
        stage_peak_prediction["maximum_dbfs"] = max(stage_peak_prediction["dbfs"].values())
        stage_peak_prediction["safe"] = stage_peak_prediction["maximum_dbfs"] <= _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS
    if not stage_peak_prediction["safe"]:
        logger.error(
            "Auto-sub: blocked unsafe sweep candidate stage=%s delay=%.2f predicted=%s",
            stage, delay_ms, stage_peak_prediction["dbfs"],
        )
        job["message"] = (
            f"AutoGain candidate blocked before sweep: predicted Stage peak "
            f"{stage_peak_prediction['maximum_dbfs']:.2f} dBFS exceeds −1 dBFS"
        )
        peak_failure = {"predicted": stage_peak_prediction, "status": "headroom_blocked"}
        job.setdefault("auto_gain", {})["stage_output_peaks"] = peak_failure
        raise AutoSubPeakSafetyError(job["message"])
    sweep_id = ""
    stage_peak_comparison: dict[str, Any] | None = None
    previous_exact_sub_mute = False
    exact_sub_mute_enabled = False
    try:
        if exact_sub_mute:
            if subwoofer_runtime is None:
                raise RuntimeError("Subwoofer runtime unavailable; exact digital mute cannot be enabled")
            previous_exact_sub_mute = await subwoofer_runtime.set_exact_sub_mute(True)
            exact_sub_mute_enabled = True
            if not subwoofer_runtime.snapshot().get("exact_sub_mute"):
                raise RuntimeError("Subwoofer helper did not retain exact digital mute state")
        if subwoofer_runtime is None:
            raise RuntimeError("Native Stage1 output peak capture unavailable")
        await subwoofer_runtime.reset_output_peaks()
        sweep_job = await measurement_store.start_measurement(
            input_id=input_id,
            channel=channel,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref,
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            sweep_profile=auto_sub_sweep_profile,
            measurement_scope="raw_helper",
        )
        sweep_id = sweep_job["id"]
        job["current_sweep_id"] = sweep_id
        _marks["sweep_start"] = time.monotonic()

        if _auto_sub_cancel_requested(job):
            try:
                measurement_store.cancel_job(sweep_id)
            except Exception:
                pass
            job["current_sweep_id"] = ""
            return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))

        sweep_ok = False
        for _poll in range(120):
            if _auto_sub_cancel_requested(job):
                try:
                    measurement_store.cancel_job(sweep_id)
                except Exception:
                    pass
                if job.get("current_sweep_id") == sweep_id:
                    job["current_sweep_id"] = ""
                return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
            await asyncio.sleep(0.5)
            try:
                current = measurement_store.get_job(sweep_id)
            except KeyError:
                sweep_ok = True
                break
            if current.get("status") in ("completed", "failed", "cancelled"):
                sweep_ok = True
                break

        if not sweep_ok:
            logger.warning("Auto-sub: sweep %s timed out (delay %.2f ms), cancelling", sweep_id, delay_ms)
            try:
                measurement_store.cancel_job(sweep_id)
            except Exception:
                pass
            await asyncio.sleep(0.5)

        _marks["sweep_poll_done"] = time.monotonic()
        measured_stage_peaks = await subwoofer_runtime.read_output_peaks()
        stage_peak_comparison = _auto_sub_stage_peak_comparison(
            stage_peak_prediction, measured_stage_peaks,
        )
        if stage_peak_comparison["relevant_mismatch"] or not stage_peak_comparison["measured_safe"]:
            reason = (
                "Native Stage1 peak mismatch"
                if stage_peak_comparison["relevant_mismatch"]
                else "Native Stage1 measured peak exceeded −1 dBFS"
            )
            logger.error("Auto-sub stopped: %s diagnostics=%s", reason, json.dumps(stage_peak_comparison, sort_keys=True))
            job["message"] = f"Auto Sub stopped: {reason}"
            job.setdefault("auto_gain", {})["stage_output_peaks"] = stage_peak_comparison
            if job.get("current_sweep_id") == sweep_id:
                job["current_sweep_id"] = ""
            raise AutoSubPeakSafetyError(reason)
        _marks["release_start"] = time.monotonic()
        _marks["release_done"] = time.monotonic()

        try:
            final = measurement_store.get_job(sweep_id)
        except KeyError:
            if job.get("current_sweep_id") == sweep_id:
                job["current_sweep_id"] = ""
            logger.warning("Auto-sub: sweep job disappeared after completion polling: %s", sweep_id)
            return _return_candidate({
                "delay_ms": delay_ms,
                "name": str(delay_ms),
                "points": [],
                "sweep_id": sweep_id,
                "status": "cancelled",
                "error": "Sweep job disappeared",
                "scan": stage,
            })
        if final.get("status") == "completed" and final.get("result"):
            result = final["result"]
            measurement = result.get("measurement") or {}
            points = []
            for t in (measurement.get("traces") or []):
                if t.get("kind") == "sweep-response":
                    points = t.get("points") or []
                    break
            if not points:
                for t in (measurement.get("review_traces") or []):
                    points = t.get("points") or []
                    if points:
                        break
            if not points:
                logger.warning("Auto-sub: no points in sweep result for delay %.2f ms", delay_ms)
            if job.get("current_sweep_id") == sweep_id:
                job["current_sweep_id"] = ""
            analysis = measurement.get("analysis") if isinstance(measurement.get("analysis"), dict) else {}
            normalized_by_db = analysis.get("normalized_by_db")
            calibrated_points = None
            if normalized_by_db is not None:
                calibrated_points = _auto_sub_reconstruct_calibrated_points(points, normalized_by_db)
            return _return_candidate({
                "delay_ms": delay_ms,
                "name": str(delay_ms),
                "points": points,
                "sweep_id": sweep_id,
                "status": "completed",
                "scan": stage,
                "normalized_by_db": normalized_by_db,
                "calibrated_points": calibrated_points,
                "exact_sub_mute": bool(exact_sub_mute),
                "measurement_channel": str(measurement.get("channel") or channel),
                "sample_rate": analysis.get("sample_rate"),
                "stage_output_peaks": stage_peak_comparison,
            })

        error_msg = final.get("error", {}).get("detail") if isinstance(final.get("error"), dict) else str(final.get("error") or "timeout")
        logger.warning("Auto-sub: sweep failed for delay %.2f ms: %s", delay_ms, error_msg)
        if job.get("current_sweep_id") == sweep_id:
            job["current_sweep_id"] = ""
        return _return_candidate({
            "delay_ms": delay_ms,
            "name": str(delay_ms),
            "points": [],
            "sweep_id": sweep_id,
            "status": "failed",
            "error": error_msg,
            "scan": stage,
            "stage_output_peaks": stage_peak_comparison or {"predicted": stage_peak_prediction},
        })
    except AutoSubPeakSafetyError:
        raise
    except Exception as exc:
        logger.exception("Auto-sub: sweep error for delay %.2f ms", delay_ms)
        if job.get("current_sweep_id") == sweep_id:
            job["current_sweep_id"] = ""
        return _return_candidate({
            "delay_ms": delay_ms,
            "name": str(delay_ms),
            "points": [],
            "sweep_id": sweep_id,
            "status": "error",
            "error": str(exc),
            "scan": stage,
        })
    finally:
        if exact_sub_mute_enabled and subwoofer_runtime is not None:
            try:
                await subwoofer_runtime.set_exact_sub_mute(previous_exact_sub_mute)
            except Exception as exc:
                logger.exception("Auto-sub: failed to restore exact sub mute after %s reference", channel)
                job["auto_gain"] = {
                    "available": False,
                    "reason": f"Exact sub mute restore failed after Main-only {channel}: {exc}",
                }
                raise RuntimeError(
                    f"AutoSub stopped: exact sub mute restoration was not acknowledged after Main-only {channel}"
                ) from exc


def _auto_sub_reconstruct_calibrated_points(
    normalized_points: list[list[float]], normalized_by_db: Any,
) -> list[list[float]]:
    """Undo MeasurementStore normalization: normalized = raw - normalized_by."""
    offset = float(normalized_by_db)
    if not math.isfinite(offset):
        raise ValueError("normalized_by_db must be finite")
    reconstructed: list[list[float]] = []
    for point in normalized_points:
        frequency_hz, normalized_db = float(point[0]), float(point[1])
        if not math.isfinite(frequency_hz) or frequency_hz <= 0 or not math.isfinite(normalized_db):
            raise ValueError("Measurement point must contain finite positive frequency and finite dB")
        reconstructed.append([frequency_hz, round(normalized_db + offset, 3)])
    return reconstructed


def _auto_sub_log_interpolate_points(
    points: list[list[float]], frequencies_hz: list[float],
) -> list[list[float]]:
    """Interpolate dB values linearly in log-frequency, without extrapolation."""
    source = [(float(point[0]), float(point[1])) for point in points]
    if len(source) < 2:
        raise ValueError("At least two interpolation points are required")
    if any(not math.isfinite(frequency) or frequency <= 0 or not math.isfinite(db) for frequency, db in source):
        raise ValueError("Interpolation points must contain finite positive frequencies and finite dB")
    if any(source[index][0] >= source[index + 1][0] for index in range(len(source) - 1)):
        raise ValueError("Interpolation frequencies must be strictly increasing")
    result: list[list[float]] = []
    source_index = 0
    for requested in frequencies_hz:
        frequency = float(requested)
        if not math.isfinite(frequency) or frequency <= 0:
            raise ValueError("Requested interpolation frequency must be finite and positive")
        if frequency < source[0][0] or frequency > source[-1][0]:
            continue
        while source_index + 1 < len(source) and source[source_index + 1][0] < frequency:
            source_index += 1
        if source[source_index][0] == frequency:
            value = source[source_index][1]
        elif source_index + 1 < len(source) and source[source_index + 1][0] == frequency:
            value = source[source_index + 1][1]
        else:
            low_frequency, low_db = source[source_index]
            high_frequency, high_db = source[source_index + 1]
            fraction = math.log(frequency / low_frequency) / math.log(high_frequency / low_frequency)
            value = low_db + fraction * (high_db - low_db)
        result.append([frequency, round(value, 6)])
    return result


def _auto_sub_lr24_highpass_attenuation_db(
    frequency_hz: float, crossover_hz: float, sample_rate: float,
) -> float:
    """Return the exact cascaded digital Butterworth-2 high-pass response used by the helper."""
    frequency = float(frequency_hz)
    crossover = float(crossover_hz)
    rate = float(sample_rate)
    if not all(math.isfinite(value) and value > 0 for value in (frequency, crossover, rate)):
        raise ValueError("LR24 response inputs must be finite and positive")
    if frequency >= rate / 2 or crossover >= rate / 2:
        raise ValueError("LR24 response inputs must remain below Nyquist")
    omega_0 = 2.0 * math.pi * crossover / rate
    cos_0, sin_0 = math.cos(omega_0), math.sin(omega_0)
    alpha = sin_0 / (2.0 * math.sqrt(0.5))
    a0 = 1.0 + alpha
    b0 = ((1.0 + cos_0) * 0.5) / a0
    b1 = (-(1.0 + cos_0)) / a0
    b2 = b0
    a1 = (-2.0 * cos_0) / a0
    a2 = (1.0 - alpha) / a0
    omega = 2.0 * math.pi * frequency / rate
    z1 = complex(math.cos(omega), -math.sin(omega))
    z2 = z1 * z1
    one_stage = (b0 + b1 * z1 + b2 * z2) / (1.0 + a1 * z1 + a2 * z2)
    lr24_magnitude = abs(one_stage) ** 2
    return 20.0 * math.log10(max(lr24_magnitude, 1e-300))


def _auto_sub_lr24_frequency_for_attenuation(
    crossover_hz: float, sample_rate: float, max_attenuation_db: float,
) -> float:
    """Find the first frequency above XO whose digital LR24 attenuation passes the threshold."""
    threshold = float(max_attenuation_db)
    if not math.isfinite(threshold) or threshold >= 0:
        raise ValueError("LR24 attenuation threshold must be finite and below 0 dB")
    low = float(crossover_hz)
    high = min(float(sample_rate) * 0.49, max(low * 2.0, low + 1.0))
    while _auto_sub_lr24_highpass_attenuation_db(high, crossover_hz, sample_rate) < threshold:
        next_high = min(float(sample_rate) * 0.49, high * 2.0)
        if next_high <= high:
            raise ValueError("LR24 attenuation threshold is outside usable digital support")
        high = next_high
    for _ in range(64):
        middle = (low + high) * 0.5
        if _auto_sub_lr24_highpass_attenuation_db(middle, crossover_hz, sample_rate) >= threshold:
            high = middle
        else:
            low = middle
    return high


def _analyze_auto_sub_main_target_anchor(
    *, target_curve: dict[str, Any] | None, main_references: dict[str, Any] | None,
    crossover_hz: int, main_highpass_enabled: bool,
) -> dict[str, Any]:
    """Build immutable Main/Target alignment diagnostics; never calculate Gain."""
    diagnostics: dict[str, Any] = {
        "status": "unavailable",
        "reason": None,
        "method": "calibrated Main-only points with log-frequency Target interpolation",
        "gain_calculated": False,
        "crossover_frequency_hz": int(crossover_hz),
        "main_highpass_enabled": bool(main_highpass_enabled),
        "criteria": {
            "lower_bound_rule": "max(common support, exact digital LR24 frequency at -1.0 dB)" if main_highpass_enabled else "max(common support, crossover)",
            "lower_bound_justification": "With Main HP enabled, exclude points where the known helper transfer attenuates Main by more than 1.0 dB; with HP disabled, exclude below-XO bass because this gate is for Main reference above the sub integration boundary.",
            "lr24_transfer": "two cascaded Butterworth-2 high-pass biquads; |H_LR24(e^jw)|=|B(e^jw)/A(e^jw)|^2",
            "maximum_main_hp_attenuation_db": -1.0 if main_highpass_enabled else None,
            "upper_bound_rule": "minimum of actual support and 4x crossover; excludes the measured high-frequency capture floor",
            "minimum_points_per_side": 8,
            "minimum_log_span_octaves": 1.0,
            "support_qc_scope": "structural sampling adequacy only; it does not assert acoustic correctness",
            "target_extrapolation": False,
            "both_sides_required": True,
        },
        "sides": {},
    }
    if not isinstance(target_curve, dict) or len(target_curve.get("points") or []) < 2:
        diagnostics["reason"] = "Target snapshot unavailable"
        return diagnostics
    if not isinstance(main_references, dict) or main_references.get("status") != "completed":
        diagnostics["reason"] = "Completed Main-only L/R snapshots unavailable"
        return diagnostics
    target_points = target_curve.get("points") or []
    try:
        target_min = float(target_points[0][0])
        target_max = float(target_points[-1][0])
        side_points: dict[str, list[list[float]]] = {}
        sample_rates: dict[str, float] = {}
        for side in ("left", "right"):
            reference = main_references.get(side)
            points = reference.get("points") if isinstance(reference, dict) else None
            if not isinstance(reference, dict) or reference.get("status") != "completed":
                raise ValueError(f"Main-only {side} snapshot is not completed")
            if reference.get("exact_sub_mute") is not True:
                raise ValueError(f"Main-only {side} exact-sub-mute confirmation is missing")
            normalized_by_db = float(reference.get("normalized_by_db"))
            if not math.isfinite(normalized_by_db):
                raise ValueError(f"Main-only {side} normalization metadata is invalid")
            if int(reference.get("crossover_frequency_hz")) != int(crossover_hz):
                raise ValueError(f"Main-only {side} crossover does not match the AutoSub job")
            if bool(reference.get("main_highpass_enabled")) != bool(main_highpass_enabled):
                raise ValueError(f"Main-only {side} Main-HP state does not match the AutoSub job")
            sample_rate = float(reference.get("sample_rate"))
            if not math.isfinite(sample_rate) or sample_rate <= 2.0 * float(crossover_hz):
                raise ValueError(f"Main-only {side} sample-rate metadata is invalid")
            sample_rates[side] = sample_rate
            if not isinstance(points, list) or len(points) < 2:
                raise ValueError(f"Main-only {side} calibrated points unavailable")
            parsed = [[float(point[0]), float(point[1])] for point in points]
            if any(not math.isfinite(point[0]) or point[0] <= 0 or not math.isfinite(point[1]) for point in parsed):
                raise ValueError(f"Main-only {side} contains invalid points")
            if any(parsed[index][0] >= parsed[index + 1][0] for index in range(len(parsed) - 1)):
                raise ValueError(f"Main-only {side} frequencies are not strictly increasing")
            side_points[side] = parsed
    except (TypeError, ValueError, IndexError) as exc:
        diagnostics["reason"] = str(exc)
        return diagnostics

    common_low = max(target_min, side_points["left"][0][0], side_points["right"][0][0])
    common_high = min(target_max, side_points["left"][-1][0], side_points["right"][-1][0])
    if sample_rates["left"] != sample_rates["right"]:
        diagnostics["reason"] = "Main-only L/R sample rates do not match"
        return diagnostics
    if main_highpass_enabled:
        transfer_lower = _auto_sub_lr24_frequency_for_attenuation(crossover_hz, sample_rates["left"], -1.0)
        diagnostics["lr24_lower_bound_hz"] = round(transfer_lower, 6)
        diagnostics["lr24_attenuation_at_lower_bound_db"] = round(
            _auto_sub_lr24_highpass_attenuation_db(transfer_lower, crossover_hz, sample_rates["left"]), 6,
        )
        usable_low = max(common_low, transfer_lower)
    else:
        usable_low = max(common_low, float(crossover_hz))
    usable_high = min(common_high, float(crossover_hz) * 4.0)
    diagnostics["common_support_hz"] = [round(common_low, 6), round(common_high, 6)]
    diagnostics["usable_band_hz"] = [round(usable_low, 6), round(usable_high, 6)]
    if usable_high <= usable_low:
        diagnostics["reason"] = "No common Target/Main support remains in the HP/XO-safe anchor band"
        return diagnostics
    span_octaves = math.log2(usable_high / usable_low)
    diagnostics["usable_span_octaves"] = round(span_octaves, 6)
    failures: list[str] = []
    for side in ("left", "right"):
        usable_main = [point for point in side_points[side] if usable_low <= point[0] <= usable_high]
        target_on_main = _auto_sub_log_interpolate_points(target_points, [point[0] for point in usable_main])
        aligned = [
            [main_point[0], main_point[1], target_point[1]]
            for main_point, target_point in zip(usable_main, target_on_main)
        ]
        side_ok = len(aligned) >= 8 and span_octaves >= 1.0
        diagnostics["sides"][side] = {
            "status": "ready" if side_ok else "insufficient_support",
            "point_count": len(aligned),
            "frequency_support_hz": [aligned[0][0], aligned[-1][0]] if aligned else None,
            "aligned_points": aligned,
            "point_format": ["frequency_hz", "calibrated_main_db", "target_db"],
            "source": {
                "sweep_id": main_references[side].get("sweep_id"),
                "measurement_channel": main_references[side].get("measurement_channel"),
                "sample_rate": main_references[side].get("sample_rate"),
                "normalized_by_db": main_references[side].get("normalized_by_db"),
                "exact_sub_mute": True,
            },
        }
        if len(aligned) < 8:
            failures.append(f"{side} has {len(aligned)} usable points; 8 required")
    if span_octaves < 1.0:
        failures.append(f"usable span is {span_octaves:.3f} octaves; 1.0 required")
    if failures:
        diagnostics["reason"] = "; ".join(failures)
        return diagnostics
    anchor_offsets = [
        point[1] - point[2]
        for side in ("left", "right")
        for point in diagnostics["sides"][side]["aligned_points"]
    ]
    diagnostics["target_vertical_offset_db"] = round(statistics.median(anchor_offsets), 6)
    diagnostics["target_anchor_statistic"] = "median(calibrated_main_db - relative_target_db), pooled L/R"
    diagnostics["status"] = "ready"
    diagnostics["reason"] = "Anchor inputs passed"
    diagnostics["target"] = {
        "key": target_curve.get("key"),
        "label": target_curve.get("label"),
        "provenance": target_curve.get("provenance"),
    }
    return json.loads(json.dumps(diagnostics))


def _auto_sub_one_octave_smooth(points: list[list[float]]) -> list[list[float]]:
    """Robust fixed 1/1-octave smoothing using a moving median in log frequency."""
    parsed = [[float(point[0]), float(point[1])] for point in points]
    if len(parsed) < 3 or any(
        not math.isfinite(frequency) or frequency <= 0 or not math.isfinite(db)
        for frequency, db in parsed
    ):
        raise ValueError("Winner curve requires at least three finite points")
    if any(parsed[index][0] >= parsed[index + 1][0] for index in range(len(parsed) - 1)):
        raise ValueError("Winner frequencies must be strictly increasing")
    half_octave = math.sqrt(2.0)
    return [
        [frequency, round(statistics.median(
            value for neighbour_frequency, value in parsed
            if frequency / half_octave <= neighbour_frequency <= frequency * half_octave
        ), 6)]
        for frequency, _value in parsed
    ]


def _auto_sub_third_octave_smooth(points: list[list[float]]) -> list[list[float]]:
    """Robust fixed 1/3-octave smoothing for Stereo corridor checks only."""
    parsed = [[float(point[0]), float(point[1])] for point in points]
    if len(parsed) < 3 or any(
        not math.isfinite(frequency) or frequency <= 0 or not math.isfinite(db)
        for frequency, db in parsed
    ):
        raise ValueError("Corridor curve requires at least three finite points")
    if any(parsed[index][0] >= parsed[index + 1][0] for index in range(len(parsed) - 1)):
        raise ValueError("Corridor frequencies must be strictly increasing")
    half_window = 2.0 ** (1.0 / 6.0)
    return [
        [frequency, round(statistics.median(
            value for neighbour_frequency, value in parsed
            if frequency / half_window <= neighbour_frequency <= frequency * half_window
        ), 6)]
        for frequency, _value in parsed
    ]


def _auto_sub_stereo_corridor_violation(
    *, points: list[list[float]], target_curve: dict[str, Any] | None,
    anchor: dict[str, Any] | None, crossover_hz: int, direction: float,
) -> dict[str, Any]:
    """Measure broad 1/3-octave Target -6/+9 dB violations in one Gain direction."""
    result: dict[str, Any] = {
        "available": False, "relevant": False, "smoothing_octaves": 1.0 / 3.0,
        "corridor_db": [-6.0, 9.0], "direction": "lower" if direction < 0 else "raise",
        "severity_db": 0.0, "reason": None,
    }
    try:
        if not isinstance(anchor, dict) or anchor.get("status") != "ready":
            raise ValueError("Main/Target anchor is unavailable")
        offset = float(anchor.get("target_vertical_offset_db"))
        target_points = (target_curve or {}).get("points")
        if not math.isfinite(offset) or not isinstance(target_points, list) or len(target_points) < 2:
            raise ValueError("Anchored Target is unavailable")
        low, high = max(20.0, 0.5 * float(crossover_hz)), 2.0 * float(crossover_hz)
        smoothed = [point for point in _auto_sub_third_octave_smooth(points) if low <= point[0] <= high]
        target = _auto_sub_log_interpolate_points(target_points, [point[0] for point in smoothed])
        if len(smoothed) < 8 or len(target) != len(smoothed):
            raise ValueError("Stereo corridor has fewer than 8 common points")
        target = _auto_sub_third_octave_smooth(target)
        rows = []
        for measured, target_point in zip(smoothed, target):
            delta = measured[1] - (target_point[1] + offset)
            excess = max(0.0, delta - 9.0) if direction < 0 else max(0.0, -6.0 - delta)
            rows.append((measured[0], excess))
        groups: list[list[tuple[float, float]]] = []
        previous_index = -2
        for index, row in enumerate(rows):
            if row[1] > 0.0:
                if index != previous_index + 1:
                    groups.append([])
                groups[-1].append(row)
                previous_index = index
        relevant_groups = [
            group for group in groups
            if len(group) >= 3 and math.log2(group[-1][0] / group[0][0]) >= (1.0 / 6.0)
        ]
        severity = max((statistics.median(value for _frequency, value in group) for group in relevant_groups), default=0.0)
        result.update({
            "available": True, "relevant": bool(relevant_groups), "severity_db": round(severity, 3),
            "point_count": len(smoothed), "frequency_range_hz": [round(smoothed[0][0], 3), round(smoothed[-1][0], 3)],
            "broad_group_count": len(relevant_groups),
            "reason": "Broad 1/3-octave corridor violation" if relevant_groups else "No broad 1/3-octave corridor violation",
        })
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        result["reason"] = str(exc)
    return result


def _auto_sub_stereo_probe_plan(
    *, correction_plan: dict[str, Any], gain_after: dict[str, Any],
    gain_deltas: dict[str, float], accepted_step1_sides: dict[str, bool],
    after_points: dict[str, list[list[float]]], target_curve: dict[str, Any] | None,
    anchor: dict[str, Any] | None, crossover_hz: int,
) -> dict[str, Any]:
    """Plan one bounded Stereo-only probe when only the >6 dB correction limit failed."""
    result: dict[str, Any] = {"available": False, "deltas_db": {}, "channels": {}, "reason": None}
    if correction_plan.get("reason") != "Measured final Gain correction is implausible":
        result["reason"] = "Stereo probe requires only the >6 dB correction limit to have failed"
        return result
    for side in ("left", "right"):
        channel = ((correction_plan.get("channels") or {}).get(side) or {})
        try:
            response = float(channel.get("response_change_per_db"))
            remaining = float(((gain_after.get("channels") or {}).get(side) or {}).get("target_delta_db"))
            first_step = float(gain_deltas.get(side, 0.0))
            raw_correction = remaining / response
            direction_clear = (
                accepted_step1_sides.get(side) is True
                and 0.2 <= response <= 2.0
                and abs(raw_correction) > 6.0
                and raw_correction * first_step > 0.0
                and remaining * first_step > 0.0
            )
            corridor = _auto_sub_stereo_corridor_violation(
                points=after_points.get(side) or [], target_curve=target_curve, anchor=anchor,
                crossover_hz=crossover_hz, direction=raw_correction,
            )
            eligible = bool(direction_clear and corridor.get("available") and corridor.get("relevant"))
            result["channels"][side] = {
                "eligible": eligible, "response_change_per_db": round(response, 4),
                "remaining_error_db": round(remaining, 3), "raw_correction_db": round(raw_correction, 3),
                "direction_clear": direction_clear, "corridor_before": corridor,
            }
            if eligible:
                result["deltas_db"][side] = -1.0 if raw_correction < 0.0 else 1.0
        except (TypeError, ValueError, ZeroDivisionError):
            result["channels"][side] = {"eligible": False, "reason": "Incomplete Stereo probe inputs"}
    result["available"] = bool(result["deltas_db"])
    result["reason"] = "Bounded Stereo corridor probe planned" if result["available"] else "No Stereo side qualified for a bounded corridor probe"
    return result


def _calculate_auto_sub_gain(
    *, mode: str, target_curve: dict[str, Any] | None, anchor: dict[str, Any] | None,
    winner_curves: dict[str, list[list[float]]], crossover_hz: int,
) -> dict[str, Any]:
    """Calculate bounded diagnostic Gain from calibrated accepted-winner curves."""
    result: dict[str, Any] = {
        "available": False, "gain_calculated": False, "applied": False,
        "method": "fixed 1/1-octave moving-median smoothing of Winner and anchored Target; median Target-minus-Winner deviation",
        "smoothing_octaves": 1.0,
        "bounds_db": [-6.0, 6.0], "reason": None, "channels": {},
    }
    try:
        if not isinstance(anchor, dict) or anchor.get("status") != "ready":
            raise ValueError("Main/Target anchor is unavailable")
        vertical_offset = float(anchor.get("target_vertical_offset_db"))
        if not math.isfinite(vertical_offset):
            raise ValueError("Target vertical offset is invalid")
        target_points = (target_curve or {}).get("points")
        if not isinstance(target_points, list) or len(target_points) < 2:
            raise ValueError("Target snapshot is unavailable")
        requested_low = max(20.0, float(crossover_hz) * 0.5)
        requested_high = float(crossover_hz) * 2.0
        for channel, points in winner_curves.items():
            smoothed = _auto_sub_one_octave_smooth(points)
            usable = [point for point in smoothed if requested_low <= point[0] <= requested_high]
            target_on_winner = _auto_sub_log_interpolate_points(target_points, [point[0] for point in usable])
            if len(target_on_winner) != len(usable) or len(usable) < 8:
                raise ValueError(f"{channel} winner/Target support has fewer than 8 common points")
            smoothed_target = _auto_sub_one_octave_smooth(target_on_winner)
            deviations = [
                (target_point[1] + vertical_offset) - winner_point[1]
                for winner_point, target_point in zip(usable, smoothed_target)
            ]
            target_delta = statistics.median(deviations)
            raw_gain = target_delta
            mad = statistics.median(abs(value - target_delta) for value in deviations)
            bounded = min(6.0, max(-6.0, raw_gain))
            coverage_octaves = math.log2(usable[-1][0] / usable[0][0])
            confidence = "high" if len(usable) >= 24 and coverage_octaves >= 1.5 and mad <= 1.5 else (
                "medium" if len(usable) >= 12 and coverage_octaves >= 1.0 and mad <= 3.0 else "low"
            )
            result["channels"][channel] = {
                "frequency_range_hz": [round(usable[0][0], 3), round(usable[-1][0], 3)],
                "point_count": len(usable), "coverage_octaves": round(coverage_octaves, 3),
                "target_delta_db": round(target_delta, 3),
                "raw_recommendation_db": round(raw_gain, 3), "recommendation_db": round(bounded, 3),
                "clamped": bounded != raw_gain, "median_absolute_deviation_db": round(mad, 3),
                "confidence": confidence,
                "reason": f"{len(usable)} points across {coverage_octaves:.2f} octaves; MAD {mad:.2f} dB",
            }
        if mode in (OUTPUT_MODE_SUBWOOFER_21, OUTPUT_MODE_SUBWOOFER_22):
            channel_values = [entry["raw_recommendation_db"] for entry in result["channels"].values()]
            if not channel_values:
                raise ValueError("No accepted Winner channels are available")
            raw_common = statistics.median(channel_values)
            bounded_common = min(6.0, max(-6.0, raw_common))
            result["recommendation"] = {
                "type": "common", "raw_delta_db": round(raw_common, 3),
                "delta_db": round(bounded_common, 3), "clamped": bounded_common != raw_common,
                "preserves_relative_sub_gain": mode == OUTPUT_MODE_SUBWOOFER_22,
            }
        else:
            result["recommendation"] = {
                "type": "per_channel",
                "left_delta_db": result["channels"]["left"]["recommendation_db"],
                "right_delta_db": result["channels"]["right"]["recommendation_db"],
            }
        confidences = [entry["confidence"] for entry in result["channels"].values()]
        result["confidence"] = "low" if "low" in confidences else ("medium" if "medium" in confidences else "high")
        result["available"] = True
        result["gain_calculated"] = True
        result["reason"] = "Diagnostic recommendation calculated; no audio state changed"
    except (TypeError, ValueError, IndexError, KeyError) as exc:
        result["reason"] = str(exc)
    return json.loads(json.dumps(result))


def _auto_sub_gain_deltas(
    auto_gain: dict[str, Any], mode: str, *, max_abs_db: float = 6.0,
) -> dict[str, float]:
    """Use the residual only as direction/magnitude input for one bounded feedback step."""
    if not isinstance(auto_gain, dict) or not auto_gain.get("gain_calculated"):
        return {}
    limit = abs(float(max_abs_db))
    if not math.isfinite(limit) or limit <= 0:
        return {}
    bounded = lambda value: min(limit, max(-limit, float(value)))
    recommendation = auto_gain.get("recommendation") or {}
    if mode == OUTPUT_MODE_SUBWOOFER_22_STEREO:
        return {
            "left": bounded(recommendation["left_delta_db"]),
            "right": bounded(recommendation["right_delta_db"]),
        }
    delta = bounded(recommendation["delta_db"])
    return {"left": delta, "right": delta}


def _auto_sub_gain_verdict(before: dict[str, Any], after: dict[str, Any], mode: str) -> dict[str, Any]:
    """Accept one Gain attempt unless its residual Target error grows by >0.25 dB."""
    verdict = {"accepted": False, "reason": None, "channels": {}}
    if not before.get("gain_calculated") or not after.get("gain_calculated"):
        verdict["reason"] = "Gain verification inputs unavailable"
        return verdict
    names = ("left", "right")
    accepted = True
    for name in names:
        before_error = abs(float(before["channels"][name]["raw_recommendation_db"]))
        after_error = abs(float(after["channels"][name]["raw_recommendation_db"]))
        channel_ok = after_error <= before_error + 0.25
        accepted = accepted and channel_ok
        verdict["channels"][name] = {
            "before_absolute_residual_db": round(before_error, 3),
            "after_absolute_residual_db": round(after_error, 3),
            "accepted": channel_ok,
        }
    verdict["accepted"] = accepted
    verdict["reason"] = "Gain verification passed" if accepted else "After residual exceeded pre-Gain residual by more than 0.25 dB"
    return verdict


def _auto_sub_gain_response_correction(
    before: dict[str, Any], after: dict[str, Any], applied_step: dict[str, float], mode: str,
) -> dict[str, Any]:
    """Estimate one final correction from the measured broad-band response per applied dB."""
    result: dict[str, Any] = {
        "available": False, "deltas_db": {}, "raw_deltas_db": {}, "applied_deltas_db": {},
        "channels": {}, "reason": None,
    }
    if not before.get("gain_calculated") or not after.get("gain_calculated"):
        result["reason"] = "Gain response inputs unavailable"
        return result
    try:
        for side in ("left", "right"):
            step = float(applied_step[side])
            if abs(step) < 0.05:
                raise ValueError(f"{side} first Gain step is too small to measure sensitivity")
            before_delta = float(before["channels"][side]["target_delta_db"])
            after_delta = float(after["channels"][side]["target_delta_db"])
            response_change = before_delta - after_delta
            sensitivity = response_change / step
            plausible = math.isfinite(sensitivity) and 0.2 <= sensitivity <= 2.0
            result["channels"][side] = {
                "before_target_delta_db": round(before_delta, 3),
                "after_target_delta_db": round(after_delta, 3),
                "applied_step_db": round(step, 3),
                "response_change_db": round(response_change, 3),
                "response_change_per_db": round(sensitivity, 4),
                "plausible": plausible,
            }
            if not plausible:
                raise ValueError(f"{side} measured response per dB is implausible ({sensitivity:.3f})")
        if mode in (OUTPUT_MODE_SUBWOOFER_21, OUTPUT_MODE_SUBWOOFER_22):
            sensitivity = statistics.median(
                result["channels"][side]["response_change_per_db"] for side in ("left", "right")
            )
            remaining = statistics.median(
                float(after["channels"][side]["target_delta_db"]) for side in ("left", "right")
            )
            correction = remaining / sensitivity
            deltas = {"left": correction, "right": correction}
        else:
            deltas = {
                side: float(after["channels"][side]["target_delta_db"])
                / float(result["channels"][side]["response_change_per_db"])
                for side in ("left", "right")
            }
        result["raw_deltas_db"] = {side: round(value, 3) for side, value in deltas.items()}
        if any(not math.isfinite(value) or abs(value) > 6.0 for value in deltas.values()):
            raise ValueError("Measured final Gain correction is implausible")
        applied_deltas = {
            side: max(-6.0 - float(applied_step[side]), min(6.0 - float(applied_step[side]), value))
            for side, value in deltas.items()
        }
        result["applied_deltas_db"] = {side: round(value, 3) for side, value in applied_deltas.items()}
        result["deltas_db"] = dict(result["applied_deltas_db"])
        result["available"] = True
        result["reason"] = "Final correction derived from measured Before/After response"
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        result["reason"] = str(exc)
    return result


def _auto_sub_gain_log_score(diagnostics: dict[str, Any] | None) -> float | None:
    channels = (diagnostics or {}).get("channels") or {}
    values = [abs(float((channels.get(side) or {}).get("target_delta_db"))) for side in ("left", "right")]
    return round(statistics.median(values), 3) if len(values) == 2 and all(math.isfinite(v) for v in values) else None


def _auto_sub_gain_log_line(event: str, payload: dict[str, Any]) -> None:
    logger.info("%s %s", event, json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _auto_sub_22_snapshot_with_gain(
    snapshot: dict[str, Any], *, left_delta_db: float, right_delta_db: float,
) -> dict[str, Any]:
    updated = _auto_sub_snapshot_copy(snapshot)
    subs = updated.setdefault("subwoofers", {})
    for key, delta in (("sub1", left_delta_db), ("sub2", right_delta_db)):
        sub = subs.setdefault(key, {})
        sub["level_db"] = max(-80.0, min(12.0, float(sub.get("level_db", 0.0) or 0.0) + float(delta)))
    return updated


async def _capture_auto_sub_main_references(
    *,
    job: dict[str, Any],
    fc: int,
    input_id: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    auto_sub_sweep_profile: dict[str, Any],
    auto_sub_rate: int,
    output_mode: str,
    original_config_snapshot: dict[str, Any],
) -> None:
    """Capture exactly one L and one R Main-only reference before candidate scans."""
    subwoofer = original_config_snapshot.get("subwoofer") if isinstance(original_config_snapshot.get("subwoofer"), dict) else {}
    sub1 = _auto_sub_22_sub(original_config_snapshot, "sub1")
    sub2 = _auto_sub_22_sub(original_config_snapshot, "sub2")
    is_22 = output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES
    main_highpass_enabled = bool(
        _auto_sub_22_global_config(original_config_snapshot).get("main_highpass_enabled", True)
        if is_22 else subwoofer.get("main_highpass_enabled", True)
    )
    left_delay = float(sub1.get("alignment_ms", 0.0) or 0.0) if is_22 else float(subwoofer.get("sub_alignment_ms", 0.0) or 0.0)
    right_delay = float(sub2.get("alignment_ms", 0.0) or 0.0) if is_22 else left_delay
    job["stage"] = "main_reference"
    job["main_references"] = {
        "status": "running",
        "exact_sub_mute": True,
        "crossover_frequency_hz": int(fc),
        "main_highpass_enabled": main_highpass_enabled,
        "left": {"status": "pending"},
        "right": {"status": "pending"},
    }
    results: dict[str, dict[str, Any]] = {}
    for index, side in enumerate(("left", "right"), start=1):
        if _auto_sub_cancel_requested(job):
            job["main_references"][side] = {"status": "cancelled"}
            break
        delay = left_delay if side == "left" else right_delay
        result = await _measure_auto_sub_candidate(
            delay_ms=delay,
            job=job,
            candidate_index=index,
            total=2,
            stage="main_reference",
            fc=fc,
            input_id=input_id,
            channel=side,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref,
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            auto_sub_sweep_profile=auto_sub_sweep_profile,
            auto_sub_rate=auto_sub_rate,
            original_level=float(subwoofer.get("sub_level_db", 0.0) or 0.0),
            original_polarity=str(subwoofer.get("sub_polarity") or "normal"),
            original_highpass=main_highpass_enabled,
            measurement_label=f"Main-only reference: {side.title()} ({index}/2)",
            measure_channel=side,
            output_mode=output_mode,
            original_config_snapshot=original_config_snapshot,
            sub1_alignment_ms=left_delay,
            sub2_alignment_ms=right_delay,
            active_subs=("sub1", "sub2"),
            exact_sub_mute=True,
        )
        results[side] = result
        job["main_references"][side] = {
            "status": result.get("status"),
            "points": json.loads(json.dumps(result.get("calibrated_points"))) if result.get("calibrated_points") else [],
            "normalized_by_db": result.get("normalized_by_db"),
            "sweep_id": result.get("sweep_id"),
            "channel": side,
            "measurement_channel": result.get("measurement_channel"),
            "sample_rate": result.get("sample_rate"),
            "crossover_frequency_hz": int(fc),
            "main_highpass_enabled": main_highpass_enabled,
            "exact_sub_mute": bool(result.get("exact_sub_mute")),
        }
        if result.get("error"):
            job["main_references"][side]["error"] = str(result.get("error"))
    complete = len(results) == 2 and all(
        result.get("status") == "completed" and len(result.get("calibrated_points") or []) >= 2
        for result in results.values()
    )
    job["main_references"]["status"] = "completed" if complete else "unavailable"
    if not complete:
        failures = [f"{side}: {result.get('status')} ({result.get('error') or 'no calibrated points'})" for side, result in results.items() if result.get("status") != "completed" or not result.get("calibrated_points")]
        if len(results) < 2:
            failures.append("reference capture cancelled before both sides completed")
        job["auto_gain"] = {"available": False, "reason": "Main-only reference unavailable: " + "; ".join(failures)}
    job["main_target_anchor"] = _analyze_auto_sub_main_target_anchor(
        target_curve=job.get("target_curve"),
        main_references=job.get("main_references"),
        crossover_hz=fc,
        main_highpass_enabled=main_highpass_enabled,
    )
    if job["main_target_anchor"].get("status") == "ready":
        job["auto_gain"] = {
            "available": False,
            "reason": "Main/Target anchor ready; Gain calculation is not implemented",
        }
    elif complete:
        job["auto_gain"] = {
            "available": False,
            "reason": "Main/Target anchor unavailable: " + str(job["main_target_anchor"].get("reason") or "unknown reason"),
        }
async def _measure_auto_sub_combined_candidate(
    *,
    delay_ms: float,
    job: dict[str, Any],
    candidate_index: int,
    total: int,
    sweep_index_start: int,
    sweep_total: int,
    stage: str,
    fc: int,
    input_id: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    auto_sub_sweep_profile: dict[str, Any],
    auto_sub_rate: int,
    original_level: float,
    original_polarity: str,
    original_highpass: bool,
    output_mode: str = OUTPUT_MODE_SUBWOOFER_21,
    original_config_snapshot: dict[str, Any] | None = None,
    sub1_alignment_ms: float | None = None,
    sub2_alignment_ms: float | None = None,
    active_subs: tuple[str, ...] = ("sub1",),
    sub1_polarity: str | None = None,
    sub2_polarity: str | None = None,
) -> dict[str, Any]:
    """Measure both L and R for one AutoSub delay candidate."""
    _combined_start = time.monotonic()

    def _last_sweep_timing(channel_name: str) -> dict[str, Any] | None:
        for timing in reversed(job.get("_sweep_timings", [])):
            if (
                timing.get("channel") == channel_name
                and timing.get("stage") == stage
                and round(float(timing.get("delay_ms", -9999)), 2) == round(float(delay_ms), 2)
            ):
                return timing
        return None

    def _append_combined_timing(status: str, left_result: dict[str, Any] | None = None, right_result: dict[str, Any] | None = None) -> None:
        left_timing = _last_sweep_timing("left")
        right_timing = _last_sweep_timing("right")
        job.setdefault("_combined_candidate_timings", []).append({
            "delay_ms": delay_ms,
            "stage": stage,
            "candidate": candidate_index,
            "status": status,
            "left_status": (left_result or {}).get("status"),
            "right_status": (right_result or {}).get("status"),
            "left_total_ms": ((left_timing or {}).get("durations", {}) or {}).get("total_ms"),
            "right_total_ms": ((right_timing or {}).get("durations", {}) or {}).get("total_ms"),
            "total_ms": round((time.monotonic() - _combined_start) * 1000, 1),
        })

    if _auto_sub_cancel_requested(job):
        _append_combined_timing("cancelled")
        return _auto_sub_cancelled_candidate(delay_ms, stage)

    if stage == "sub1_coarse":
        label = "Optimizing Sub 1"
    elif stage == "sub2_coarse":
        label = "Optimizing Sub 2"
    elif stage == "combined_matrix":
        label = "Combined Matrix"
    else:
        label = "Fine-Scan" if stage == "fine" else "Coarse scan"
    pair_suffix = ""
    if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
        s1 = _auto_sub_clamped_delay(sub1_alignment_ms if sub1_alignment_ms is not None else delay_ms)
        s2 = _auto_sub_clamped_delay(sub2_alignment_ms if sub2_alignment_ms is not None else 0.0)
        pair_suffix = f" (S1 {s1:.2f} ms / S2 {s2:.2f} ms)"
    left_result = await _measure_auto_sub_candidate(
        delay_ms=delay_ms,
        job=job,
        candidate_index=sweep_index_start,
        total=sweep_total,
        stage=stage,
        fc=fc,
        input_id=input_id,
        channel="left",
        mic_input_channel=mic_input_channel,
        reference_input_channel=reference_input_channel,
        calibration_ref=calibration_ref,
        calibration_filename=calibration_filename,
        calibration_bytes=calibration_bytes,
        auto_sub_sweep_profile=auto_sub_sweep_profile,
        auto_sub_rate=auto_sub_rate,
        original_level=original_level,
        original_polarity=original_polarity,
        original_highpass=original_highpass,
        measurement_label=f"{label}: L meas {candidate_index}/{total} @ {delay_ms:.2f} ms{pair_suffix}",
        candidate_current=candidate_index,
        candidate_total=total,
        measure_channel="left",
        output_mode=output_mode,
        original_config_snapshot=original_config_snapshot,
        sub1_alignment_ms=sub1_alignment_ms,
        sub2_alignment_ms=sub2_alignment_ms,
        active_subs=active_subs,
        sub1_polarity=sub1_polarity,
        sub2_polarity=sub2_polarity,
    )
    if _auto_sub_cancel_requested(job):
        _append_combined_timing("cancelled", left_result=left_result)
        return _auto_sub_cancelled_candidate(delay_ms, stage)

    right_result = await _measure_auto_sub_candidate(
        delay_ms=delay_ms,
        job=job,
        candidate_index=sweep_index_start + 1,
        total=sweep_total,
        stage=stage,
        fc=fc,
        input_id=input_id,
        channel="right",
        mic_input_channel=mic_input_channel,
        reference_input_channel=reference_input_channel,
        calibration_ref=calibration_ref,
        calibration_filename=calibration_filename,
        calibration_bytes=calibration_bytes,
        auto_sub_sweep_profile=auto_sub_sweep_profile,
        auto_sub_rate=auto_sub_rate,
        original_level=original_level,
        original_polarity=original_polarity,
        original_highpass=original_highpass,
        measurement_label=f"{label}: R meas {candidate_index}/{total} @ {delay_ms:.2f} ms{pair_suffix}",
        candidate_current=candidate_index,
        candidate_total=total,
        measure_channel="right",
        output_mode=output_mode,
        original_config_snapshot=original_config_snapshot,
        sub1_alignment_ms=sub1_alignment_ms,
        sub2_alignment_ms=sub2_alignment_ms,
        active_subs=active_subs,
        sub1_polarity=sub1_polarity,
        sub2_polarity=sub2_polarity,
    )
    if _auto_sub_cancel_requested(job):
        _append_combined_timing("cancelled", left_result=left_result, right_result=right_result)
        return _auto_sub_cancelled_candidate(delay_ms, stage)

    left_points = left_result.get("points") or []
    right_points = right_result.get("points") or []
    points = left_points if len(left_points) >= 3 else right_points
    status = "completed" if (len(left_points) >= 3 or len(right_points) >= 3) else "failed"
    _append_combined_timing(status, left_result=left_result, right_result=right_result)

    candidate = {
        "delay_ms": delay_ms,
        "name": str(delay_ms),
        "points": points,
        "points_left": left_points,
        "points_right": right_points,
        "calibrated_points_left": left_result.get("calibrated_points") or [],
        "calibrated_points_right": right_result.get("calibrated_points") or [],
        "normalized_by_db_left": left_result.get("normalized_by_db"),
        "normalized_by_db_right": right_result.get("normalized_by_db"),
        "sweep_id": left_result.get("sweep_id", ""),
        "sweep_id_left": left_result.get("sweep_id", ""),
        "sweep_id_right": right_result.get("sweep_id", ""),
        "status": status,
        "scan": stage,
        "status_left": left_result.get("status"),
        "status_right": right_result.get("status"),
        "stage_output_peaks": {
            "left": left_result.get("stage_output_peaks"),
            "right": right_result.get("stage_output_peaks"),
        },
        "combined_candidate": True,
    }
    if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
        sub1_delay = _auto_sub_clamped_delay(sub1_alignment_ms if sub1_alignment_ms is not None else delay_ms)
        sub2_delay = _auto_sub_clamped_delay(sub2_alignment_ms if sub2_alignment_ms is not None else 0.0)
        candidate.update({
            "sub1_alignment_ms": sub1_delay,
            "sub2_alignment_ms": sub2_delay,
            "name": _auto_sub_22_name(sub1_delay, sub2_delay),
            "active_subs": list(active_subs),
            "sub1_polarity": sub1_polarity,
            "sub2_polarity": sub2_polarity,
        })
    return candidate



def _finalize_autosub_job(job: dict[str, Any] | None, job_id: str) -> None:
    """Transition an AutoSub job from cancelling to cancelled and log cleanup."""
    if job is None:
        logger.warning("AUTOSUB job=%s worker finished (job missing)", job_id)
        return
    was_cancelling = str(job.get("status") or "").lower() == "cancelling"
    was_cancel_requested = bool(job.get("cancel_requested"))
    if was_cancelling or was_cancel_requested:
        job["status"] = "cancelled"
        job["message"] = "Auto Sub Optimize cancelled."
        job["cancel_requested"] = True
        logger.info("AUTOSUB job=%s worker finished (was cancelling=%s)", job_id, was_cancelling)
    if isinstance(job.get("result"), dict):
        job["result"]["target_curve"] = json.loads(json.dumps(job.get("target_curve"))) if job.get("target_curve") else None
        job["result"]["auto_gain"] = json.loads(json.dumps(job.get("auto_gain")))
        job["result"]["main_references"] = json.loads(json.dumps(job.get("main_references"))) if job.get("main_references") else None
        job["result"]["main_target_anchor"] = json.loads(json.dumps(job.get("main_target_anchor"))) if job.get("main_target_anchor") else None
        job["result"]["polarity_check"] = json.loads(json.dumps(job.get("polarity_check"))) if job.get("polarity_check") else None
    logger.info("AUTOSUB job=%s cleanup complete state=%s", job_id, job.get("status") or "idle")


async def _run_auto_sub_22_optimize(
    job_id: str,
    input_id: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    sub1_scan_delays: list[float],
    sub2_scan_delays: list[float],
    fc: int,
    original_config_snapshot: dict[str, Any],
    fine_step_ms: float,
) -> None:
    global measurement_store, subwoofer_runtime, _auto_sub_lock
    from samplerate import _load_audio_output_mode, set_audio_output_mode

    job = _AUTO_SUB_JOBS.get(job_id)
    if not job:
        _auto_sub_lock.release()
        return

    async def _restore_original_config() -> None:
        await _restore_auto_sub_original_config(original_config_snapshot)

    original_sub1 = _auto_sub_22_sub(original_config_snapshot, "sub1")
    original_sub2 = _auto_sub_22_sub(original_config_snapshot, "sub2")
    original_sub1_alignment = float(original_sub1.get("alignment_ms", 0.0) or 0.0)
    original_sub2_alignment = float(original_sub2.get("alignment_ms", 0.0) or 0.0)

    def _matrix_delays(center: float) -> list[float]:
        return [_auto_sub_clamped_delay(center + offset) for offset in (-fine_step_ms, 0.0, fine_step_ms)]

    def _valid_lr(result: dict[str, Any]) -> bool:
        return _auto_sub_has_points(result, "points_left") or _auto_sub_has_points(result, "points_right")

    def _same_pair(pair: tuple[float, float], sub1_alignment: float, sub2_alignment: float) -> bool:
        return abs(pair[0] - sub1_alignment) <= 0.05 and abs(pair[1] - sub2_alignment) <= 0.05

    if measurement_sr_session is not None:
        await measurement_sr_session.register_auto_sub(job_id)
    try:
        if _auto_sub_cancel_requested(job):
            logger.info("AUTOSUB job=%s cancel observed (before sweeps)", job_id)
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        auto_sub_sweep_low_hz = 20.0
        auto_sub_sweep_high_hz = max(600.0, min(float(fc) * 8.0, 2000.0))
        if fc <= 60:
            auto_sub_sweep_sec, auto_sub_tail_sec = 3.5, 1.5
        elif fc <= 120:
            auto_sub_sweep_sec, auto_sub_tail_sec = 3.0, 1.3
        else:
            auto_sub_sweep_sec, auto_sub_tail_sec = 2.5, 1.1
        auto_sub_sweep_profile = {
            "sweep_start_hz": auto_sub_sweep_low_hz,
            "sweep_end_hz": auto_sub_sweep_high_hz,
            "sweep_seconds": auto_sub_sweep_sec,
            "tail_seconds": auto_sub_tail_sec,
        }
        auto_sub_rate = _resolve_measurement_start_sample_rate()
        await _capture_auto_sub_main_references(
            job=job, fc=fc, input_id=input_id,
            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
            auto_sub_rate=auto_sub_rate, output_mode=OUTPUT_MODE_SUBWOOFER_22,
            original_config_snapshot=original_config_snapshot,
        )
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        coarse1_results: list[dict[str, Any]] = []
        coarse2_results: list[dict[str, Any]] = []
        matrix_results: list[dict[str, Any]] = []
        sub1_sweep_total = len(sub1_scan_delays) * 2
        sub2_sweep_total = len(sub2_scan_delays) * 2
        matrix_sweep_start = sub1_sweep_total + sub2_sweep_total
        matrix_sweep_total = matrix_sweep_start + 18

        job["stage"] = "sub1_coarse"
        for idx, delay_ms in enumerate(sub1_scan_delays):
            coarse1_results.append(await _measure_auto_sub_combined_candidate(
                delay_ms=delay_ms,
                job=job,
                candidate_index=idx + 1,
                total=len(sub1_scan_delays),
                sweep_index_start=(idx * 2) + 1,
                sweep_total=matrix_sweep_total,
                stage="sub1_coarse",
                fc=fc,
                input_id=input_id,
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate,
                original_level=0.0,
                original_polarity="normal",
                original_highpass=True,
                output_mode=OUTPUT_MODE_SUBWOOFER_22,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=delay_ms,
                sub2_alignment_ms=original_sub2_alignment,
                active_subs=("sub1",),
            ))
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        coarse1_valid = [result for result in coarse1_results if _valid_lr(result)]
        if not coarse1_valid:
            job["status"] = "failed"
            job["message"] = "No valid Sub 1 coarse sweep results to score"
            job["error"] = {"detail": "Sub 1 coarse sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return
        sub1_scoring = _score_auto_sub_combined_candidates(
            coarse1_results,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_sub1_alignment,
        )
        sub1_winner = sub1_scoring["winner"]
        sub1_winner_delay = _auto_sub_clamped_delay(float(sub1_winner.get("delay_ms", original_sub1_alignment) or original_sub1_alignment))

        job["stage"] = "sub2_coarse"
        for idx, delay_ms in enumerate(sub2_scan_delays):
            coarse2_results.append(await _measure_auto_sub_combined_candidate(
                delay_ms=delay_ms,
                job=job,
                candidate_index=idx + 1,
                total=len(sub2_scan_delays),
                sweep_index_start=sub1_sweep_total + (idx * 2) + 1,
                sweep_total=matrix_sweep_total,
                stage="sub2_coarse",
                fc=fc,
                input_id=input_id,
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate,
                original_level=0.0,
                original_polarity="normal",
                original_highpass=True,
                output_mode=OUTPUT_MODE_SUBWOOFER_22,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=original_sub1_alignment,
                sub2_alignment_ms=delay_ms,
                active_subs=("sub2",),
            ))
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        coarse2_valid = [result for result in coarse2_results if _valid_lr(result)]
        if not coarse2_valid:
            job["status"] = "failed"
            job["message"] = "No valid Sub 2 coarse sweep results to score"
            job["error"] = {"detail": "Sub 2 coarse sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return
        sub2_scoring = _score_auto_sub_combined_candidates(
            coarse2_results,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_sub2_alignment,
        )
        sub2_winner = sub2_scoring["winner"]
        sub2_winner_delay = _auto_sub_clamped_delay(float(sub2_winner.get("delay_ms", original_sub2_alignment) or original_sub2_alignment))

        sub1_matrix = _matrix_delays(sub1_winner_delay)
        sub2_matrix = _matrix_delays(sub2_winner_delay)
        matrix_pairs = [(sub1_delay, sub2_delay) for sub1_delay in sub1_matrix for sub2_delay in sub2_matrix]
        incumbent_pair = (
            _auto_sub_clamped_delay(original_sub1_alignment),
            _auto_sub_clamped_delay(original_sub2_alignment),
        )
        incumbent_in_matrix = any(_same_pair(pair, incumbent_pair[0], incumbent_pair[1]) for pair in matrix_pairs)
        if not incumbent_in_matrix:
            matrix_pairs.append(incumbent_pair)
        job["combined_matrix"] = {
            "status": "running",
            "fine_step_ms": fine_step_ms,
            "sub1_candidates": sub1_matrix,
            "sub2_candidates": sub2_matrix,
            "incumbent_pair": {"sub1_alignment_ms": incumbent_pair[0], "sub2_alignment_ms": incumbent_pair[1]},
            "incumbent_in_matrix": incumbent_in_matrix,
            "candidates": [
                {
                    "sub1_alignment_ms": a,
                    "sub2_alignment_ms": b,
                    "incumbent_pair": _same_pair((a, b), incumbent_pair[0], incumbent_pair[1]),
                }
                for a, b in matrix_pairs
            ],
        }
        matrix_sweep_total = matrix_sweep_start + (len(matrix_pairs) * 2)

        job["stage"] = "combined_matrix"
        for idx, (sub1_delay, sub2_delay) in enumerate(matrix_pairs):
            matrix_results.append(await _measure_auto_sub_combined_candidate(
                delay_ms=sub1_delay,
                job=job,
                candidate_index=idx + 1,
                total=len(matrix_pairs),
                sweep_index_start=matrix_sweep_start + (idx * 2) + 1,
                sweep_total=matrix_sweep_total,
                stage="combined_matrix",
                fc=fc,
                input_id=input_id,
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate,
                original_level=0.0,
                original_polarity="normal",
                original_highpass=True,
                output_mode=OUTPUT_MODE_SUBWOOFER_22,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=sub1_delay,
                sub2_alignment_ms=sub2_delay,
                active_subs=("sub1", "sub2"),
            ))
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        matrix_valid = [result for result in matrix_results if _valid_lr(result)]
        if not matrix_valid:
            job["status"] = "failed"
            job["message"] = "No valid Combined Matrix sweep results to score"
            job["error"] = {"detail": "Combined Matrix sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return

        matrix_scoring = _score_auto_sub_matrix_candidates(
            matrix_results,
            crossover_hz=fc,
            original_sub1_alignment_ms=original_sub1_alignment,
            original_sub2_alignment_ms=original_sub2_alignment,
        )
        winner = matrix_scoring["accepted_winner"]
        gain_winner = next(
            (candidate for candidate in matrix_results
             if round(float(candidate.get("sub1_alignment_ms", 0.0)), 2) == round(float(winner.get("sub1_alignment_ms", 0.0)), 2)
             and round(float(candidate.get("sub2_alignment_ms", 0.0)), 2) == round(float(winner.get("sub2_alignment_ms", 0.0)), 2)),
            {},
        )
        best_sub1 = _auto_sub_clamped_delay(float(winner.get("sub1_alignment_ms", sub1_winner_delay) or sub1_winner_delay))
        best_sub2 = _auto_sub_clamped_delay(float(winner.get("sub2_alignment_ms", sub2_winner_delay) or sub2_winner_delay))
        incumbent_polarities = (str(original_sub1.get("polarity", "normal")), str(original_sub2.get("polarity", "normal")))
        selected_polarities = incumbent_polarities
        polarity_candidates: list[dict[str, Any]] = [dict(gain_winner, delay_ms=0.0)]
        alternative_polarities = [
            (_auto_sub_opposite_polarity(incumbent_polarities[0]), incumbent_polarities[1]),
            (incumbent_polarities[0], _auto_sub_opposite_polarity(incumbent_polarities[1])),
            (_auto_sub_opposite_polarity(incumbent_polarities[0]), _auto_sub_opposite_polarity(incumbent_polarities[1])),
        ]
        for idx, polarities in enumerate(alternative_polarities, 1):
            measured = await _measure_auto_sub_combined_candidate(
                delay_ms=best_sub1, job=job, candidate_index=idx, total=3,
                sweep_index_start=matrix_sweep_total + (idx - 1) * 2 + 1,
                sweep_total=matrix_sweep_total + 6, stage="polarity_check", fc=fc,
                input_id=input_id, mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                original_level=0.0, original_polarity="normal", original_highpass=True,
                output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                active_subs=("sub1", "sub2"), sub1_polarity=polarities[0], sub2_polarity=polarities[1],
            )
            polarity_candidates.append(dict(measured, delay_ms=float(idx), tested_polarities=polarities))
        polarity_scoring = _score_auto_sub_combined_candidates(polarity_candidates, crossover_hz=fc, low_guard_reference_delay_ms=0.0)
        polarity_winner = polarity_scoring["winner"]
        incumbent_scored = _auto_sub_result_for_delay(polarity_scoring["results"], 0.0) or {}
        alternative_scored = polarity_winner if float(polarity_winner.get("delay_ms", 0.0)) != 0.0 else {}
        polarity_decision = _auto_sub_polarity_decision(incumbent_scored, alternative_scored) if alternative_scored else {
            "accepted": False, "reason": "incumbent_best", "score_gain": 0.0, "min_score_gain": 0.03,
        }
        if polarity_decision["accepted"]:
            selected_idx = int(round(float(alternative_scored["delay_ms"])))
            selected_polarities = alternative_polarities[selected_idx - 1]
            selected_measurement = polarity_candidates[selected_idx]
            refinement: list[dict[str, Any]] = []
            refinements = [(a, b) for a in _matrix_delays(best_sub1) for b in _matrix_delays(best_sub2)]
            for idx, (delay1, delay2) in enumerate(refinements):
                refinement.append(await _measure_auto_sub_combined_candidate(
                    delay_ms=delay1, job=job, candidate_index=idx + 1, total=9,
                    sweep_index_start=matrix_sweep_total + 7 + idx * 2,
                    sweep_total=matrix_sweep_total + 24, stage="polarity_refine", fc=fc,
                    input_id=input_id, mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                    original_level=0.0, original_polarity="normal", original_highpass=True,
                    output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=original_config_snapshot,
                    sub1_alignment_ms=delay1, sub2_alignment_ms=delay2, active_subs=("sub1", "sub2"),
                    sub1_polarity=selected_polarities[0], sub2_polarity=selected_polarities[1],
                ))
            refined_scoring = _score_auto_sub_matrix_candidates(refinement, crossover_hz=fc)
            refined_winner = refined_scoring["winner"]
            best_sub1 = float(refined_winner["sub1_alignment_ms"])
            best_sub2 = float(refined_winner["sub2_alignment_ms"])
            gain_winner = next((row for row in refinement if abs(float(row.get("sub1_alignment_ms", 0))-best_sub1)<0.01 and abs(float(row.get("sub2_alignment_ms", 0))-best_sub2)<0.01), selected_measurement)
            polarity_decision["refinement"] = {"winner": refined_winner, "candidate_count": 9}
        polarity_snapshot = _auto_sub_snapshot_copy(original_config_snapshot)
        polarity_snapshot.setdefault("subwoofers", {}).setdefault("sub1", {})["polarity"] = selected_polarities[0]
        polarity_snapshot.setdefault("subwoofers", {}).setdefault("sub2", {})["polarity"] = selected_polarities[1]
        job["polarity_check"] = {**polarity_decision, "incumbent": incumbent_polarities, "selected": selected_polarities, "alternatives_tested": alternative_polarities}
        job["auto_gain"] = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_22,
            target_curve=job.get("target_curve"), anchor=job.get("main_target_anchor"),
            winner_curves={
                "left": gain_winner.get("calibrated_points_left") or [],
                "right": gain_winner.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        logger.info("AUTOSUB_GAIN mode=2.2_mono diagnostics=%s", json.dumps(job["auto_gain"], sort_keys=True))
        gain_deltas = _auto_sub_gain_deltas(job["auto_gain"], OUTPUT_MODE_SUBWOOFER_22, max_abs_db=6.0)
        _auto_sub_gain_log_line("AUTOGAIN_INIT", {
            "mode": OUTPUT_MODE_SUBWOOFER_22, "xo_hz": fc,
            "target": (job.get("target_curve") or {}).get("label"),
            "anchor_hz": (job.get("main_target_anchor") or {}).get("usable_band_hz"),
            "target_offset_db": (job.get("main_target_anchor") or {}).get("target_vertical_offset_db"),
            "gain_before": {"sub1": float(original_sub1.get("level_db", 0.0)), "sub2": float(original_sub2.get("level_db", 0.0))},
            "winner_delta_left": (job["auto_gain"].get("channels", {}).get("left") or {}).get("target_delta_db"),
            "winner_delta_right": (job["auto_gain"].get("channels", {}).get("right") or {}).get("target_delta_db"),
            "combined_delta_db": (job["auto_gain"].get("recommendation") or {}).get("raw_delta_db"),
            "first_step_db": gain_deltas.get("left"),
        })
        gain_snapshot = _auto_sub_22_snapshot_with_gain(
            polarity_snapshot,
            left_delta_db=gain_deltas.get("left", 0.0), right_delta_db=gain_deltas.get("right", 0.0),
        )
        candidate_ledger = (
            _auto_sub_candidate_ledger(
                coarse1_results, sub1_scoring, mode="2.2_mono", phase="sub1_coarse",
                roles={"coarse_winner": sub1_winner},
            )
            + _auto_sub_candidate_ledger(
                coarse2_results, sub2_scoring, mode="2.2_mono", phase="sub2_coarse",
                roles={"coarse_winner": sub2_winner},
            )
            + _auto_sub_candidate_ledger(
                matrix_results, matrix_scoring, mode="2.2_mono", phase="matrix",
                roles={
                    "matrix_winner": matrix_scoring.get("matrix_winner"),
                    "final_accepted_winner": winner,
                },
                requested_incumbent={
                    "sub1_alignment_ms": original_sub1_alignment,
                    "sub2_alignment_ms": original_sub2_alignment,
                },
            )
        )

        apply_ok = False
        try:
            sub_config = _auto_sub_22_global_config(gain_snapshot)
            subwoofers_config = _auto_sub_22_candidate_subwoofers(
                gain_snapshot,
                sub1_alignment_ms=best_sub1,
                sub2_alignment_ms=best_sub2,
                active_subs=("sub1", "sub2"),
            )
            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_22, sub_config, subwoofers_config)
            if subwoofer_runtime is not None:
                config = SubwooferRuntimeConfig.from_overview(get_audio_output_overview())
                await subwoofer_runtime.sync(config)
            await asyncio.sleep(0.3)
            verify = _load_audio_output_mode()
            apply_ok = _auto_sub_22_verify_alignment(verify, best_sub1, best_sub2)
        except Exception:
            logger.exception("Auto-sub 2.2: failed to apply winner pair %.2f / %.2f ms", best_sub1, best_sub2)

        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        if not apply_ok:
            job["status"] = "failed"
            job["message"] = f"Scoring succeeded but failed to apply winner pair {best_sub1:.2f} / {best_sub2:.2f} ms"
            job["error"] = {"detail": "Winner apply failed - original config restored"}
            await _restore_original_config()
            return

        gain_after_sweep = await _measure_auto_sub_combined_candidate(
            delay_ms=best_sub1, job=job, candidate_index=1, total=1,
            sweep_index_start=matrix_sweep_total + 1, sweep_total=matrix_sweep_total + 2,
            stage="gain_after", fc=fc, input_id=input_id,
            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
            auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
            original_highpass=bool(_auto_sub_22_global_config(gain_snapshot).get("main_highpass_enabled", True)),
            output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=gain_snapshot,
            sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2, active_subs=("sub1", "sub2"),
        )
        gain_after = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_22, target_curve=job.get("target_curve"),
            anchor=job.get("main_target_anchor"), winner_curves={
                "left": gain_after_sweep.get("calibrated_points_left") or [],
                "right": gain_after_sweep.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        gain_verdict = _auto_sub_gain_verdict(job["auto_gain"], gain_after, OUTPUT_MODE_SUBWOOFER_22)
        final_gain_deltas = gain_deltas if gain_verdict["accepted"] else {"left": 0.0, "right": 0.0}
        final_gain_snapshot = gain_snapshot if gain_verdict["accepted"] else polarity_snapshot
        final_gain_sweep = gain_after_sweep if gain_verdict["accepted"] else gain_winner
        correction_deltas: dict[str, float] = {}
        correction_plan = None
        correction_after = None
        correction_verdict = None
        if not gain_verdict["accepted"]:
            rollback_subs = _auto_sub_22_candidate_subwoofers(
                polarity_snapshot, sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                active_subs=("sub1", "sub2"),
            )
            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_22, _auto_sub_22_global_config(polarity_snapshot), rollback_subs)
            if subwoofer_runtime is not None:
                await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        else:
            correction_plan = _auto_sub_gain_response_correction(
                job["auto_gain"], gain_after, gain_deltas, OUTPUT_MODE_SUBWOOFER_22,
            )
            correction_deltas = correction_plan.get("deltas_db") or {}
            correction_delta = correction_deltas.get("left", 0.0)
            if not correction_plan.get("available"):
                correction_verdict = {
                    "accepted": False,
                    "reason": correction_plan.get("reason"),
                    "channels": {},
                    "step1_retained": True,
                }
            elif abs(correction_delta) > 0.0005:
                correction_snapshot = _auto_sub_22_snapshot_with_gain(
                    gain_snapshot, left_delta_db=correction_delta, right_delta_db=correction_delta,
                )
                set_audio_output_mode(
                    OUTPUT_MODE_SUBWOOFER_22, _auto_sub_22_global_config(correction_snapshot),
                    _auto_sub_22_candidate_subwoofers(
                        correction_snapshot, sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                        active_subs=("sub1", "sub2"),
                    ),
                )
                if subwoofer_runtime is not None:
                    await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
                correction_sweep = await _measure_auto_sub_combined_candidate(
                    delay_ms=best_sub1, job=job, candidate_index=1, total=1,
                    sweep_index_start=matrix_sweep_total + 3, sweep_total=matrix_sweep_total + 4,
                    stage="gain_correction_after", fc=fc, input_id=input_id,
                    mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
                    original_highpass=bool(_auto_sub_22_global_config(correction_snapshot).get("main_highpass_enabled", True)),
                    output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=correction_snapshot,
                    sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2, active_subs=("sub1", "sub2"),
                )
                correction_after = _calculate_auto_sub_gain(
                    mode=OUTPUT_MODE_SUBWOOFER_22, target_curve=job.get("target_curve"),
                    anchor=job.get("main_target_anchor"), winner_curves={
                        "left": correction_sweep.get("calibrated_points_left") or [],
                        "right": correction_sweep.get("calibrated_points_right") or [],
                    }, crossover_hz=fc,
                )
                correction_verdict = _auto_sub_gain_verdict(gain_after, correction_after, OUTPUT_MODE_SUBWOOFER_22)
                if correction_verdict["accepted"]:
                    final_gain_deltas = {
                        "left": gain_deltas.get("left", 0.0) + correction_delta,
                        "right": gain_deltas.get("right", 0.0) + correction_delta,
                    }
                    final_gain_snapshot = correction_snapshot
                    final_gain_sweep = correction_sweep
                else:
                    set_audio_output_mode(
                        OUTPUT_MODE_SUBWOOFER_22, _auto_sub_22_global_config(gain_snapshot),
                        _auto_sub_22_candidate_subwoofers(
                            gain_snapshot, sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                            active_subs=("sub1", "sub2"),
                        ),
                    )
                    if subwoofer_runtime is not None:
                        await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        _auto_sub_gain_log_line("AUTOGAIN_FEEDBACK", {
            "gain_after_step1": {
                "sub1": float(_auto_sub_22_sub(gain_snapshot, "sub1").get("level_db", 0.0)),
                "sub2": float(_auto_sub_22_sub(gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "score_before": _auto_sub_gain_log_score(job["auto_gain"]),
            "score_after_step1": _auto_sub_gain_log_score(gain_after),
            "response_per_db_left": (((correction_plan or {}).get("channels") or {}).get("left") or {}).get("response_change_per_db"),
            "response_per_db_right": (((correction_plan or {}).get("channels") or {}).get("right") or {}).get("response_change_per_db"),
            "remaining_error_left": (gain_after.get("channels", {}).get("left") or {}).get("target_delta_db"),
            "remaining_error_right": (gain_after.get("channels", {}).get("right") or {}).get("target_delta_db"),
            "raw_correction_db": ((correction_plan or {}).get("raw_deltas_db") or {}).get("left"),
            "applied_correction_db": ((correction_plan or {}).get("applied_deltas_db") or {}).get("left"),
            "correction_step_db": correction_deltas.get("left") if correction_deltas else None,
        })
        decision = "accepted_step2" if correction_verdict and correction_verdict.get("accepted") else (
            "accepted_step1" if gain_verdict.get("accepted") else "restored"
        )
        score_final_source = correction_after if decision == "accepted_step2" else (gain_after if decision == "accepted_step1" else job["auto_gain"])
        _auto_sub_gain_log_line("AUTOGAIN_RESULT", {
            "gain_final": {
                "sub1": float(_auto_sub_22_sub(final_gain_snapshot, "sub1").get("level_db", 0.0)),
                "sub2": float(_auto_sub_22_sub(final_gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "score_final": _auto_sub_gain_log_score(score_final_source), "decision": decision,
            "reason": ((correction_verdict or gain_verdict) or {}).get("reason"),
            "delay_final": {"sub1_ms": best_sub1, "sub2_ms": best_sub2},
        })
        job["auto_gain"].update({
            "applied": bool(gain_verdict["accepted"] and gain_deltas),
            "reverted": bool(gain_deltas and not gain_verdict["accepted"]),
            "verification": gain_after, "verification_verdict": gain_verdict,
            "response_correction": correction_plan,
            "correction_deltas_db": correction_deltas,
            "correction_verification": correction_after,
            "correction_verdict": correction_verdict,
            "final_deltas_db": final_gain_deltas,
            "original_levels_db": {
                "sub1": float(original_sub1.get("level_db", 0.0)), "sub2": float(original_sub2.get("level_db", 0.0)),
            },
            "final_levels_db": {
                "sub1": float(_auto_sub_22_sub(final_gain_snapshot, "sub1").get("level_db", 0.0)),
                "sub2": float(_auto_sub_22_sub(final_gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "stage_output_peaks": (final_gain_sweep or {}).get("stage_output_peaks"),
        })

        derived_delays: dict[str, Any] = {}
        try:
            config = SubwooferRuntimeConfig.from_overview(get_audio_output_overview())
            derived_delays = {
                "derived_main_delay_ms": round(config.derived_main_delay_ms, 2),
                "derived_sub1_delay_ms": round(config.derived_sub1_delay_ms, 2),
                "derived_sub2_delay_ms": round(config.derived_sub2_delay_ms, 2),
            }
        except Exception:
            derived_delays = {}

        job["combined_matrix"].update({
            "status": "completed",
            "winner": winner,
            "matrix_winner": matrix_scoring.get("matrix_winner"),
            "incumbent_winner": matrix_scoring.get("incumbent_winner"),
            "incumbent_score": matrix_scoring.get("incumbent_score"),
            "accepted_winner": matrix_scoring.get("accepted_winner"),
            "incumbent_accepted": matrix_scoring.get("incumbent_accepted"),
            "reject_reason": matrix_scoring.get("reject_reason"),
            "runner_up": matrix_scoring.get("runner_up"),
            "results": matrix_scoring["results"],
            "valid_count": len(matrix_valid),
        })
        _log_auto_sub_timing_summary(job)

        # Build baseline and confirmation measurements for before/after graph display
        all_22_sweeps = list(coarse1_results) + list(coarse2_results) + list(matrix_results)
        baseline_22_sweep = next(
            (r for r in all_22_sweeps
             if round(float(r.get("sub1_alignment_ms", r.get("delay_ms", 0.0))), 2) == round(float(original_sub1_alignment), 2)
             and round(float(r.get("sub2_alignment_ms", 0.0)), 2) == round(float(original_sub2_alignment), 2)
             and (_auto_sub_has_points(r, "points_left") or _auto_sub_has_points(r, "points_right"))),
            None,
        )
        confirm_22_sweep = final_gain_sweep if gain_verdict["accepted"] else next(
            (r for r in all_22_sweeps
             if round(float(r.get("sub1_alignment_ms", r.get("delay_ms", 0.0))), 2) == round(float(best_sub1), 2)
             and round(float(r.get("sub2_alignment_ms", 0.0)), 2) == round(float(best_sub2), 2)
             and (_auto_sub_has_points(r, "points_left") or _auto_sub_has_points(r, "points_right"))),
            None,
        )
        baseline_measurement = None
        confirmation_measurement = None
        _offset_db = _auto_sub_shared_bass_offset(
            baseline_22_sweep.get("points_left") if baseline_22_sweep else [],
            baseline_22_sweep.get("points_right") if baseline_22_sweep else [],
        )
        if baseline_22_sweep:
            baseline_measurement = _auto_sub_measurement_from_sweep(
                baseline_22_sweep, "Before", f"AutoSub 2.2 Baseline (S1 {original_sub1_alignment:.1f} / S2 {original_sub2_alignment:.1f} ms)",
                offset_db=_offset_db,
            )
        if confirm_22_sweep:
            confirmation_measurement = _auto_sub_measurement_from_sweep(
                confirm_22_sweep, "After", f"AutoSub 2.2 Optimized (S1 {best_sub1:.1f} / S2 {best_sub2:.1f} ms)",
                offset_db=_offset_db,
            )

        job["status"] = "completed"
        decision_label = "Kept 2.2 incumbent" if matrix_scoring.get("incumbent_accepted") else "Applied 2.2"
        job["message"] = (
            f"{decision_label}: Sub 1 {best_sub1:.2f} ms / Sub 2 {best_sub2:.2f} ms "
            f"(score {winner['score_pct']:.0f} %, {matrix_scoring.get('reject_reason')})"
        )
        job["result"] = {
            "mode": OUTPUT_MODE_SUBWOOFER_22,
            "original_sub1_alignment_ms": original_sub1_alignment,
            "original_sub2_alignment_ms": original_sub2_alignment,
            "suggested_sub1_alignment_ms": best_sub1,
            "suggested_sub2_alignment_ms": best_sub2,
            "applied_sub1_alignment_ms": best_sub1,
            "applied_sub2_alignment_ms": best_sub2,
            "applied": True,
            "auto_applied": True,
            "apply_decision": (
                "kept_22_incumbent"
                if matrix_scoring.get("incumbent_accepted")
                else "applied_22_combined_matrix"
            ),
            "crossover_hz": fc,
            "confidence": matrix_scoring.get("confidence", "uncertain"),
            "winner": winner,
            "matrix_winner": matrix_scoring.get("matrix_winner"),
            "incumbent_winner": matrix_scoring.get("incumbent_winner"),
            "incumbent_score": matrix_scoring.get("incumbent_score"),
            "accepted_winner": matrix_scoring.get("accepted_winner"),
            "incumbent_accepted": matrix_scoring.get("incumbent_accepted"),
            "reject_reason": matrix_scoring.get("reject_reason"),
            "sub1_coarse_winner": sub1_winner,
            "sub2_coarse_winner": sub2_winner,
            "runner_up": matrix_scoring.get("runner_up"),
            "ranking": matrix_scoring["results"],
            "combined_matrix": job["combined_matrix"],
            "candidate_ledger": candidate_ledger,
            "sweep_count": matrix_sweep_total + 2,
            "candidate_count": len(sub1_scan_delays) + len(sub2_scan_delays) + len(matrix_pairs),
            "sub1_coarse_candidate_count": len(sub1_scan_delays),
            "sub2_coarse_candidate_count": len(sub2_scan_delays),
            "matrix_candidate_count": len(matrix_pairs),
            "valid_count": len(matrix_valid),
            "sub1_coarse_valid_count": len(coarse1_valid),
            "sub2_coarse_valid_count": len(coarse2_valid),
            "baseline_measurement": baseline_measurement,
            "confirmation_measurement": confirmation_measurement,
            **derived_delays,
        }
        logger.info(
            "Auto-sub 2.2 optimize completed: fc=%sHz sub1 %.2f->%.2fms sub2 %.2f->%.2fms "
            "combined_score=%.0f%% score_L=%.1f%% score_R=%.1f%% confidence=%s",
            fc,
            original_sub1_alignment,
            best_sub1,
            original_sub2_alignment,
            best_sub2,
            winner.get("score_pct", 0),
            winner.get("score_L_pct", 0) or 0,
            winner.get("score_R_pct", 0) or 0,
            matrix_scoring.get("confidence", "uncertain"),
        )

    except Exception as exc:
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        logger.exception("Auto-sub 2.2 optimize failed")
        job["status"] = "failed"
        job["message"] = f"Auto Sub Optimize 2.2 failed: {exc}"
        job["error"] = {"detail": str(exc)}
        await _restore_original_config()

    finally:
        if measurement_sr_session is not None:
            await measurement_sr_session.unregister_auto_sub(job_id)
        _finalize_autosub_job(job, job_id)
        try:
            _auto_sub_lock.release()
        except RuntimeError:
            pass
        cleanup_job_id = job_id

        async def _cleanup_autosub_job():
            await asyncio.sleep(600)
            _AUTO_SUB_JOBS.pop(cleanup_job_id, None)

        asyncio.create_task(_cleanup_autosub_job())


async def _run_auto_sub_22_stereo_optimize(
    job_id: str,
    input_id: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    left_scan_delays: list[float],
    right_scan_delays: list[float],
    fc: int,
    original_config_snapshot: dict[str, Any],
) -> None:
    global measurement_store, subwoofer_runtime, _auto_sub_lock
    from samplerate import _load_audio_output_mode, set_audio_output_mode

    job = _AUTO_SUB_JOBS.get(job_id)
    if not job:
        _auto_sub_lock.release()
        return

    async def _restore_original_config() -> None:
        await _restore_auto_sub_original_config(original_config_snapshot)

    original_left = _auto_sub_22_sub(original_config_snapshot, "sub1")
    original_right = _auto_sub_22_sub(original_config_snapshot, "sub2")
    original_left_alignment = float(original_left.get("alignment_ms", 0.0) or 0.0)
    original_right_alignment = float(original_right.get("alignment_ms", 0.0) or 0.0)

    def _valid(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [result for result in results if _auto_sub_has_points(result, "points")]

    def _reference_points(results: list[dict[str, Any]], delay_ms: float) -> list[list[float]] | None:
        valid = _valid(results)
        if not valid:
            return None
        reference = min(valid, key=lambda result: abs(float(result.get("delay_ms", 0.0) or 0.0) - delay_ms))
        points = reference.get("points") or []
        return points if isinstance(points, list) and len(points) >= 3 else None

    if measurement_sr_session is not None:
        await measurement_sr_session.register_auto_sub(job_id)
    try:
        if _auto_sub_cancel_requested(job):
            logger.info("AUTOSUB job=%s cancel observed (before sweeps)", job_id)
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        auto_sub_sweep_low_hz = 20.0
        auto_sub_sweep_high_hz = max(600.0, min(float(fc) * 8.0, 2000.0))
        if fc <= 60:
            auto_sub_sweep_sec, auto_sub_tail_sec = 3.5, 1.5
        elif fc <= 120:
            auto_sub_sweep_sec, auto_sub_tail_sec = 3.0, 1.3
        else:
            auto_sub_sweep_sec, auto_sub_tail_sec = 2.5, 1.1
        auto_sub_sweep_profile = {
            "sweep_start_hz": auto_sub_sweep_low_hz,
            "sweep_end_hz": auto_sub_sweep_high_hz,
            "sweep_seconds": auto_sub_sweep_sec,
            "tail_seconds": auto_sub_tail_sec,
        }
        auto_sub_rate = _resolve_measurement_start_sample_rate()
        await _capture_auto_sub_main_references(
            job=job, fc=fc, input_id=input_id,
            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
            auto_sub_rate=auto_sub_rate, output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
            original_config_snapshot=original_config_snapshot,
        )
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        step_ms = _auto_sub_step_ms(fc)
        planned_left_fine_total = 6
        planned_right_fine_total = 6
        planned_sweep_total = (
            len(left_scan_delays)
            + planned_left_fine_total
            + len(right_scan_delays)
            + planned_right_fine_total
        )

        left_results: list[dict[str, Any]] = []
        job["stage"] = "left_sub"
        for idx, delay_ms in enumerate(left_scan_delays):
            sweep_index = idx + 1
            left_results.append(await _measure_auto_sub_candidate(
                delay_ms=delay_ms,
                job=job,
                candidate_index=sweep_index,
                total=planned_sweep_total,
                stage="left_sub",
                fc=fc,
                input_id=input_id,
                channel="left",
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate,
                original_level=0.0,
                original_polarity="normal",
                original_highpass=True,
                measurement_label=f"Optimizing Left Sub: L sweep {idx + 1}/{len(left_scan_delays)} @ {delay_ms:.2f} ms",
                candidate_current=idx + 1,
                candidate_total=len(left_scan_delays),
                measure_channel="left",
                output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=delay_ms,
                sub2_alignment_ms=original_right_alignment,
                active_subs=("sub1",),
            ))
            if isinstance(job.get("progress"), dict):
                job["progress"]["sweep_current"] = sweep_index
                job["progress"]["sweep_total"] = planned_sweep_total
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        left_valid = _valid(left_results)
        if not left_valid:
            job["status"] = "failed"
            job["message"] = "No valid Left Sub sweep results to score"
            job["error"] = {"detail": "Left Sub sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return
        left_coarse_scoring = score_sub_alignment_candidates(
            left_valid,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_left_alignment,
        )
        _auto_sub_rank_results(left_coarse_scoring["results"])
        left_coarse_winner = left_coarse_scoring["winner"]
        left_coarse_runner_up = left_coarse_scoring.get("runner_up")
        left_fine_delays = _auto_sub_fine_delay_candidates(
            left_coarse_winner,
            left_coarse_runner_up,
            step_ms,
            {round(float(delay), 2) for delay in left_scan_delays},
        )
        left_fine_results: list[dict[str, Any]] = []
        left_fine_valid: list[dict[str, Any]] = []
        left_fine_scoring: dict[str, Any] | None = None
        left_fine_winner: dict[str, Any] | None = None
        left_low_guard_reference_points = _reference_points(left_valid, original_left_alignment)
        job["fine_scan"] = {
            "enabled": True,
            "triggered": bool(left_fine_delays),
            "status": "left_running" if left_fine_delays else "left_skipped",
            "fine_step_ms": step_ms / 4.0,
            "left": {
                "status": "running" if left_fine_delays else "skipped",
                "coarse_winner": left_coarse_winner,
                "coarse_runner_up": left_coarse_runner_up,
                "candidates": left_fine_delays,
            },
            "right": {"status": "pending", "candidates": []},
        }
        if left_fine_delays:
            job["stage"] = "left_fine"
            for idx, delay_ms in enumerate(left_fine_delays):
                sweep_index = len(left_scan_delays) + idx + 1
                left_fine_results.append(await _measure_auto_sub_candidate(
                    delay_ms=delay_ms,
                    job=job,
                    candidate_index=sweep_index,
                    total=planned_sweep_total,
                    stage="left_fine",
                    fc=fc,
                    input_id=input_id,
                    channel="left",
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate,
                    original_level=0.0,
                    original_polarity="normal",
                    original_highpass=True,
                    measurement_label=f"Optimizing Left Sub Fine: L sweep {idx + 1}/{len(left_fine_delays)} @ {delay_ms:.2f} ms",
                    candidate_current=idx + 1,
                    candidate_total=len(left_fine_delays),
                    measure_channel="left",
                    output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                    original_config_snapshot=original_config_snapshot,
                    sub1_alignment_ms=delay_ms,
                    sub2_alignment_ms=original_right_alignment,
                    active_subs=("sub1",),
                ))
                if isinstance(job.get("progress"), dict):
                    job["progress"]["sweep_current"] = sweep_index
                    job["progress"]["sweep_total"] = planned_sweep_total
                if _auto_sub_cancel_requested(job):
                    job["message"] = "Auto Sub Optimize cancelled."
                    await _restore_original_config()
                    return
            left_fine_valid = _valid(left_fine_results)
            if left_fine_valid:
                left_fine_scoring = score_sub_alignment_candidates(
                    left_fine_valid,
                    crossover_hz=fc,
                    low_guard_reference_points=left_low_guard_reference_points,
                    low_guard_reference_delay_ms=original_left_alignment,
                )
                _auto_sub_rank_results(left_fine_scoring["results"])
                left_fine_winner = left_fine_scoring["winner"]
                job["fine_scan"]["left"].update({
                    "status": "completed",
                    "winner": left_fine_winner,
                    "runner_up": left_fine_scoring.get("runner_up"),
                    "results": left_fine_scoring["results"],
                    "valid_count": len(left_fine_valid),
                    "sweep_count": len(left_fine_delays),
                })
            else:
                job["fine_scan"]["left"].update({
                    "status": "no_valid_results",
                    "winner": None,
                    "runner_up": None,
                    "results": left_fine_results,
                    "valid_count": 0,
                    "sweep_count": len(left_fine_delays),
                })

        left_final_valid = left_valid + left_fine_valid
        left_scoring = score_sub_alignment_candidates(
            left_final_valid,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_left_alignment,
        )
        _auto_sub_rank_results(left_scoring["results"])
        left_scan_by_delay: dict[float, str] = {}
        for result in left_valid:
            left_scan_by_delay[_auto_sub_delay_key(result)] = "coarse"
        for result in left_fine_valid:
            left_scan_by_delay[_auto_sub_delay_key(result)] = "fine"
        for result in left_scoring["results"]:
            result["scan"] = left_scan_by_delay.get(_auto_sub_delay_key(result), result.get("scan", "coarse"))
        left_coarse_accepted_candidate = _auto_sub_best_scan_result(left_scoring["results"], "coarse") or left_coarse_winner
        left_fine_accepted_candidate = _auto_sub_best_scan_result(left_scoring["results"], "fine")
        left_incumbent_winner = _auto_sub_result_for_delay(left_scoring["results"], original_left_alignment)
        left_acceptance = _auto_sub_select_accepted_winner(
            coarse_winner=left_coarse_accepted_candidate,
            fine_winner=left_fine_accepted_candidate,
            incumbent_winner=left_incumbent_winner,
        )
        left_winner = left_acceptance["accepted_winner"]
        best_left = _auto_sub_clamped_delay(float(left_winner.get("delay_ms", original_left_alignment) or original_left_alignment))
        job["fine_scan"]["left"]["final_winner"] = left_winner
        job["fine_scan"]["left"]["final_results"] = left_scoring["results"]
        job["fine_scan"]["left"]["accepted_winner"] = left_winner
        job["fine_scan"]["left"]["fine_accepted"] = left_acceptance["fine_accepted"]
        job["fine_scan"]["left"]["reject_reason"] = left_acceptance["reject_reason"]
        job["fine_scan"]["left"]["incumbent_winner"] = left_incumbent_winner
        job["fine_scan"]["left"]["incumbent_score"] = left_acceptance["incumbent_score"]
        job["fine_scan"]["status"] = "right_pending"

        right_results: list[dict[str, Any]] = []
        job["stage"] = "right_sub"
        for idx, delay_ms in enumerate(right_scan_delays):
            sweep_index = len(left_scan_delays) + len(left_fine_delays) + idx + 1
            right_results.append(await _measure_auto_sub_candidate(
                delay_ms=delay_ms,
                job=job,
                candidate_index=sweep_index,
                total=planned_sweep_total,
                stage="right_sub",
                fc=fc,
                input_id=input_id,
                channel="right",
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate,
                original_level=0.0,
                original_polarity="normal",
                original_highpass=True,
                measurement_label=f"Optimizing Right Sub: R sweep {idx + 1}/{len(right_scan_delays)} @ {delay_ms:.2f} ms",
                candidate_current=idx + 1,
                candidate_total=len(right_scan_delays),
                measure_channel="right",
                output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=best_left,
                sub2_alignment_ms=delay_ms,
                active_subs=("sub2",),
            ))
            if isinstance(job.get("progress"), dict):
                job["progress"]["sweep_current"] = sweep_index
                job["progress"]["sweep_total"] = planned_sweep_total
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        right_valid = _valid(right_results)
        if not right_valid:
            job["status"] = "failed"
            job["message"] = "No valid Right Sub sweep results to score"
            job["error"] = {"detail": "Right Sub sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return
        right_coarse_scoring = score_sub_alignment_candidates(
            right_valid,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_right_alignment,
        )
        _auto_sub_rank_results(right_coarse_scoring["results"])
        right_coarse_winner = right_coarse_scoring["winner"]
        right_coarse_runner_up = right_coarse_scoring.get("runner_up")
        right_fine_delays = _auto_sub_fine_delay_candidates(
            right_coarse_winner,
            right_coarse_runner_up,
            step_ms,
            {round(float(delay), 2) for delay in right_scan_delays},
        )
        right_fine_results: list[dict[str, Any]] = []
        right_fine_valid: list[dict[str, Any]] = []
        right_fine_scoring: dict[str, Any] | None = None
        right_fine_winner: dict[str, Any] | None = None
        right_low_guard_reference_points = _reference_points(right_valid, original_right_alignment)
        actual_sweep_total = (
            len(left_scan_delays)
            + len(left_fine_delays)
            + len(right_scan_delays)
            + len(right_fine_delays)
        )
        job["fine_scan"].update({
            "triggered": bool(left_fine_delays or right_fine_delays),
            "status": "right_running" if right_fine_delays else "right_skipped",
        })
        job["fine_scan"]["right"] = {
            "status": "running" if right_fine_delays else "skipped",
            "coarse_winner": right_coarse_winner,
            "coarse_runner_up": right_coarse_runner_up,
            "candidates": right_fine_delays,
        }
        if right_fine_delays:
            job["stage"] = "right_fine"
            for idx, delay_ms in enumerate(right_fine_delays):
                sweep_index = len(left_scan_delays) + len(left_fine_delays) + len(right_scan_delays) + idx + 1
                right_fine_results.append(await _measure_auto_sub_candidate(
                    delay_ms=delay_ms,
                    job=job,
                    candidate_index=sweep_index,
                    total=actual_sweep_total,
                    stage="right_fine",
                    fc=fc,
                    input_id=input_id,
                    channel="right",
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate,
                    original_level=0.0,
                    original_polarity="normal",
                    original_highpass=True,
                    measurement_label=f"Optimizing Right Sub Fine: R sweep {idx + 1}/{len(right_fine_delays)} @ {delay_ms:.2f} ms",
                    candidate_current=idx + 1,
                    candidate_total=len(right_fine_delays),
                    measure_channel="right",
                    output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                    original_config_snapshot=original_config_snapshot,
                    sub1_alignment_ms=best_left,
                    sub2_alignment_ms=delay_ms,
                    active_subs=("sub2",),
                ))
                if isinstance(job.get("progress"), dict):
                    job["progress"]["sweep_current"] = sweep_index
                    job["progress"]["sweep_total"] = actual_sweep_total
                if _auto_sub_cancel_requested(job):
                    job["message"] = "Auto Sub Optimize cancelled."
                    await _restore_original_config()
                    return
            right_fine_valid = _valid(right_fine_results)
            if right_fine_valid:
                right_fine_scoring = score_sub_alignment_candidates(
                    right_fine_valid,
                    crossover_hz=fc,
                    low_guard_reference_points=right_low_guard_reference_points,
                    low_guard_reference_delay_ms=original_right_alignment,
                )
                _auto_sub_rank_results(right_fine_scoring["results"])
                right_fine_winner = right_fine_scoring["winner"]
                job["fine_scan"]["right"].update({
                    "status": "completed",
                    "winner": right_fine_winner,
                    "runner_up": right_fine_scoring.get("runner_up"),
                    "results": right_fine_scoring["results"],
                    "valid_count": len(right_fine_valid),
                    "sweep_count": len(right_fine_delays),
                })
            else:
                job["fine_scan"]["right"].update({
                    "status": "no_valid_results",
                    "winner": None,
                    "runner_up": None,
                    "results": right_fine_results,
                    "valid_count": 0,
                    "sweep_count": len(right_fine_delays),
                })

        right_final_valid = right_valid + right_fine_valid
        right_scoring = score_sub_alignment_candidates(
            right_final_valid,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_right_alignment,
        )
        _auto_sub_rank_results(right_scoring["results"])
        right_scan_by_delay: dict[float, str] = {}
        for result in right_valid:
            right_scan_by_delay[_auto_sub_delay_key(result)] = "coarse"
        for result in right_fine_valid:
            right_scan_by_delay[_auto_sub_delay_key(result)] = "fine"
        for result in right_scoring["results"]:
            result["scan"] = right_scan_by_delay.get(_auto_sub_delay_key(result), result.get("scan", "coarse"))
        right_coarse_accepted_candidate = _auto_sub_best_scan_result(right_scoring["results"], "coarse") or right_coarse_winner
        right_fine_accepted_candidate = _auto_sub_best_scan_result(right_scoring["results"], "fine")
        right_incumbent_winner = _auto_sub_result_for_delay(right_scoring["results"], original_right_alignment)
        right_acceptance = _auto_sub_select_accepted_winner(
            coarse_winner=right_coarse_accepted_candidate,
            fine_winner=right_fine_accepted_candidate,
            incumbent_winner=right_incumbent_winner,
        )
        right_winner = right_acceptance["accepted_winner"]
        best_right = _auto_sub_clamped_delay(float(right_winner.get("delay_ms", original_right_alignment) or original_right_alignment))
        job["fine_scan"]["right"]["final_winner"] = right_winner
        job["fine_scan"]["right"]["final_results"] = right_scoring["results"]
        job["fine_scan"]["right"]["accepted_winner"] = right_winner
        job["fine_scan"]["right"]["fine_accepted"] = right_acceptance["fine_accepted"]
        job["fine_scan"]["right"]["reject_reason"] = right_acceptance["reject_reason"]
        job["fine_scan"]["right"]["incumbent_winner"] = right_incumbent_winner
        job["fine_scan"]["right"]["incumbent_score"] = right_acceptance["incumbent_score"]
        job["fine_scan"]["status"] = "completed"

        candidate_ledger = (
            _auto_sub_candidate_ledger(
                left_results, left_scoring, mode="2.2_stereo", phase="left_coarse", channel="left",
                roles={
                    "coarse_winner": left_coarse_accepted_candidate,
                    "final_accepted_winner": left_winner,
                },
                requested_incumbent={"delay_ms": original_left_alignment},
            )
            + _auto_sub_candidate_ledger(
                left_fine_results, left_scoring, mode="2.2_stereo", phase="left_fine", channel="left",
                roles={"fine_winner": left_fine_accepted_candidate, "final_accepted_winner": left_winner},
            )
            + _auto_sub_candidate_ledger(
                right_results, right_scoring, mode="2.2_stereo", phase="right_coarse", channel="right",
                roles={
                    "coarse_winner": right_coarse_accepted_candidate,
                    "final_accepted_winner": right_winner,
                },
                requested_incumbent={"delay_ms": original_right_alignment},
            )
            + _auto_sub_candidate_ledger(
                right_fine_results, right_scoring, mode="2.2_stereo", phase="right_fine", channel="right",
                roles={"fine_winner": right_fine_accepted_candidate, "final_accepted_winner": right_winner},
            )
        )

        apply_ok = False
        try:
            sub_config = _auto_sub_22_global_config(original_config_snapshot)
            subwoofers_config = _auto_sub_22_candidate_subwoofers(
                original_config_snapshot,
                sub1_alignment_ms=best_left,
                sub2_alignment_ms=best_right,
                active_subs=("sub1", "sub2"),
            )
            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_22_STEREO, sub_config, subwoofers_config)
            if subwoofer_runtime is not None:
                config = SubwooferRuntimeConfig.from_overview(get_audio_output_overview())
                await subwoofer_runtime.sync(config)
            await asyncio.sleep(0.3)
            apply_ok = _auto_sub_22_verify_alignment(_load_audio_output_mode(), best_left, best_right)
        except Exception:
            logger.exception("Auto-sub 2.2 Stereo Bass: failed to apply winner pair %.2f / %.2f ms", best_left, best_right)

        if not apply_ok:
            job["status"] = "failed"
            job["message"] = f"Scoring succeeded but failed to apply Left/Right pair {best_left:.2f} / {best_right:.2f} ms"
            job["error"] = {"detail": "Winner apply failed - original config restored"}
            await _restore_original_config()
            return

        derived_delays: dict[str, Any] = {}
        try:
            config = SubwooferRuntimeConfig.from_overview(get_audio_output_overview())
            derived_delays = {
                "derived_main_delay_ms": round(config.derived_main_delay_ms, 2),
                "derived_sub1_delay_ms": round(config.derived_sub1_delay_ms, 2),
                "derived_sub2_delay_ms": round(config.derived_sub2_delay_ms, 2),
            }
        except Exception:
            derived_delays = {}

        left_score = float(left_winner.get("score", 0.0) or 0.0)
        right_score = float(right_winner.get("score", 0.0) or 0.0)
        gain_left_winner = _auto_sub_result_for_delay(list(left_results) + list(left_fine_results), best_left) or {}
        gain_right_winner = _auto_sub_result_for_delay(list(right_results) + list(right_fine_results), best_right) or {}
        selected_left_polarity = str(original_left.get("polarity", "normal"))
        selected_right_polarity = str(original_right.get("polarity", "normal"))
        stereo_polarity: dict[str, Any] = {}

        async def _check_stereo_polarity(
            side: str, incumbent: dict[str, Any], delay: float, incumbent_polarity: str,
        ) -> tuple[dict[str, Any], float, str, dict[str, Any]]:
            opposite = _auto_sub_opposite_polarity(incumbent_polarity)
            is_left = side == "left"
            alt = await _measure_auto_sub_candidate(
                delay_ms=delay, job=job, candidate_index=1, total=1, stage=f"{side}_polarity_check", fc=fc,
                input_id=input_id, channel=side, mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                original_level=0.0, original_polarity="normal", original_highpass=True,
                measure_channel=side, output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=delay if is_left else best_left,
                sub2_alignment_ms=best_right if is_left else delay,
                active_subs=("sub1",) if is_left else ("sub2",),
                sub1_polarity=opposite if is_left else selected_left_polarity,
                sub2_polarity=selected_right_polarity if is_left else opposite,
            )
            rows = [dict(incumbent, delay_ms=0.0, points=incumbent.get("points") or []), dict(alt, delay_ms=1.0)]
            scoring = score_sub_alignment_candidates(rows, crossover_hz=fc, low_guard_reference_delay_ms=0.0)
            scored_incumbent = _auto_sub_result_for_delay(scoring["results"], 0.0) or {}
            scored_alt = _auto_sub_result_for_delay(scoring["results"], 1.0) or {}
            decision = _auto_sub_polarity_decision(scored_incumbent, scored_alt)
            decision.update({"incumbent": incumbent_polarity, "alternative": opposite, "selected": incumbent_polarity})
            if not decision["accepted"]:
                return incumbent, delay, incumbent_polarity, decision
            local_step = _auto_sub_step_ms(fc) / 4.0
            local_rows = [alt]
            for idx, candidate_delay in enumerate([
                _auto_sub_clamped_delay(delay - 2 * local_step), _auto_sub_clamped_delay(delay - local_step),
                _auto_sub_clamped_delay(delay + local_step), _auto_sub_clamped_delay(delay + 2 * local_step),
            ]):
                local_rows.append(await _measure_auto_sub_candidate(
                    delay_ms=candidate_delay, job=job, candidate_index=idx + 1, total=4,
                    stage=f"{side}_polarity_fine", fc=fc, input_id=input_id, channel=side,
                    mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
                    original_highpass=True, measure_channel=side,
                    output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO, original_config_snapshot=original_config_snapshot,
                    sub1_alignment_ms=candidate_delay if is_left else best_left,
                    sub2_alignment_ms=best_right if is_left else candidate_delay,
                    active_subs=("sub1",) if is_left else ("sub2",),
                    sub1_polarity=opposite if is_left else selected_left_polarity,
                    sub2_polarity=selected_right_polarity if is_left else opposite,
                ))
            fine_scoring = score_sub_alignment_candidates(local_rows, crossover_hz=fc)
            fine_winner = fine_scoring["winner"]
            selected = _auto_sub_result_for_delay(local_rows, float(fine_winner["delay_ms"])) or alt
            decision.update({"selected": opposite, "fine_scan": {"candidate_count": 4, "winner": fine_winner}})
            return selected, float(fine_winner["delay_ms"]), opposite, decision

        gain_left_winner, best_left, selected_left_polarity, stereo_polarity["left"] = await _check_stereo_polarity(
            "left", gain_left_winner, best_left, selected_left_polarity,
        )
        gain_right_winner, best_right, selected_right_polarity, stereo_polarity["right"] = await _check_stereo_polarity(
            "right", gain_right_winner, best_right, selected_right_polarity,
        )
        polarity_snapshot = _auto_sub_snapshot_copy(original_config_snapshot)
        polarity_snapshot.setdefault("subwoofers", {}).setdefault("sub1", {})["polarity"] = selected_left_polarity
        polarity_snapshot.setdefault("subwoofers", {}).setdefault("sub2", {})["polarity"] = selected_right_polarity
        job["polarity_check"] = stereo_polarity
        job["auto_gain"] = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
            target_curve=job.get("target_curve"), anchor=job.get("main_target_anchor"),
            winner_curves={
                "left": gain_left_winner.get("calibrated_points") or [],
                "right": gain_right_winner.get("calibrated_points") or [],
            }, crossover_hz=fc,
        )
        logger.info("AUTOSUB_GAIN mode=2.2_stereo diagnostics=%s", json.dumps(job["auto_gain"], sort_keys=True))
        gain_deltas = _auto_sub_gain_deltas(job["auto_gain"], OUTPUT_MODE_SUBWOOFER_22_STEREO, max_abs_db=6.0)
        _auto_sub_gain_log_line("AUTOGAIN_INIT", {
            "mode": OUTPUT_MODE_SUBWOOFER_22_STEREO, "xo_hz": fc,
            "target": (job.get("target_curve") or {}).get("label"),
            "anchor_hz": (job.get("main_target_anchor") or {}).get("usable_band_hz"),
            "target_offset_db": (job.get("main_target_anchor") or {}).get("target_vertical_offset_db"),
            "gain_before": {"left": float(original_left.get("level_db", 0.0)), "right": float(original_right.get("level_db", 0.0))},
            "winner_delta_left": (job["auto_gain"].get("channels", {}).get("left") or {}).get("target_delta_db"),
            "winner_delta_right": (job["auto_gain"].get("channels", {}).get("right") or {}).get("target_delta_db"),
            "combined_delta_db": None, "first_step_db": gain_deltas,
        })
        gain_snapshot = _auto_sub_22_snapshot_with_gain(
            polarity_snapshot,
            left_delta_db=gain_deltas.get("left", 0.0), right_delta_db=gain_deltas.get("right", 0.0),
        )
        if gain_deltas:
            set_audio_output_mode(
                OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(gain_snapshot),
                _auto_sub_22_candidate_subwoofers(
                    gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                    active_subs=("sub1", "sub2"),
                ),
            )
            if subwoofer_runtime is not None:
                await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        gain_after_left = await _measure_auto_sub_candidate(
            delay_ms=best_left, job=job, candidate_index=1, total=2, stage="gain_after", fc=fc,
            input_id=input_id, channel="left", mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
            calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
            auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
            original_level=0.0, original_polarity="normal", original_highpass=True,
            measure_channel="left", output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
            original_config_snapshot=gain_snapshot, sub1_alignment_ms=best_left,
            sub2_alignment_ms=best_right, active_subs=("sub1", "sub2"),
        )
        gain_after_right = await _measure_auto_sub_candidate(
            delay_ms=best_right, job=job, candidate_index=2, total=2, stage="gain_after", fc=fc,
            input_id=input_id, channel="right", mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
            calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
            auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
            original_level=0.0, original_polarity="normal", original_highpass=True,
            measure_channel="right", output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
            original_config_snapshot=gain_snapshot, sub1_alignment_ms=best_left,
            sub2_alignment_ms=best_right, active_subs=("sub1", "sub2"),
        )
        gain_after = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_22_STEREO, target_curve=job.get("target_curve"),
            anchor=job.get("main_target_anchor"), winner_curves={
                "left": gain_after_left.get("calibrated_points") or [],
                "right": gain_after_right.get("calibrated_points") or [],
            }, crossover_hz=fc,
        )
        gain_verdict = _auto_sub_gain_verdict(job["auto_gain"], gain_after, OUTPUT_MODE_SUBWOOFER_22_STEREO)
        accepted_step1_sides = {
            side: bool((gain_verdict.get("channels", {}).get(side) or {}).get("accepted"))
            for side in ("left", "right")
        }
        retained_step1_deltas = {
            side: gain_deltas.get(side, 0.0) if accepted_step1_sides[side] else 0.0
            for side in ("left", "right")
        }
        step1_retained = any(accepted_step1_sides.values())
        final_gain_deltas = retained_step1_deltas
        final_gain_snapshot = _auto_sub_22_snapshot_with_gain(
            polarity_snapshot,
            left_delta_db=retained_step1_deltas["left"],
            right_delta_db=retained_step1_deltas["right"],
        )
        final_gain_left = gain_after_left if accepted_step1_sides["left"] else gain_left_winner
        final_gain_right = gain_after_right if accepted_step1_sides["right"] else gain_right_winner
        correction_deltas: dict[str, float] = {}
        correction_plan = None
        correction_after = None
        correction_verdict = None
        stereo_probe_plan = None
        if not step1_retained:
            set_audio_output_mode(
                OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(original_config_snapshot),
                _auto_sub_22_candidate_subwoofers(
                    original_config_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                    active_subs=("sub1", "sub2"),
                ),
            )
            if subwoofer_runtime is not None:
                await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        elif not all(accepted_step1_sides.values()):
            # Stereo channels have independent Gain controls.  A regression on
            # one side must not discard a measured improvement on the other.
            set_audio_output_mode(
                OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(final_gain_snapshot),
                _auto_sub_22_candidate_subwoofers(
                    final_gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                    active_subs=("sub1", "sub2"),
                ),
            )
            if subwoofer_runtime is not None:
                await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
            correction_verdict = {
                "accepted": False,
                "reason": "Retained improved Stereo side; restored regressed side",
                "channels": gain_verdict.get("channels", {}),
                "step1_retained": True,
            }
        else:
            correction_plan = _auto_sub_gain_response_correction(
                job["auto_gain"], gain_after, gain_deltas, OUTPUT_MODE_SUBWOOFER_22_STEREO,
            )
            correction_deltas = correction_plan.get("deltas_db") or {}
            if not correction_plan.get("available"):
                stereo_probe_plan = _auto_sub_stereo_probe_plan(
                    correction_plan=correction_plan, gain_after=gain_after, gain_deltas=gain_deltas,
                    accepted_step1_sides=accepted_step1_sides,
                    after_points={
                        "left": gain_after_left.get("calibrated_points") or [],
                        "right": gain_after_right.get("calibrated_points") or [],
                    },
                    target_curve=job.get("target_curve"), anchor=job.get("main_target_anchor"),
                    crossover_hz=fc,
                )
                correction_deltas = stereo_probe_plan.get("deltas_db") or {}
                if not stereo_probe_plan.get("available"):
                    correction_verdict = {
                        "accepted": False,
                        "reason": correction_plan.get("reason"),
                        "channels": {},
                        "step1_retained": True,
                    }
            if any(abs(value) > 0.0005 for value in correction_deltas.values()):
                correction_snapshot = _auto_sub_22_snapshot_with_gain(
                    gain_snapshot, left_delta_db=correction_deltas.get("left", 0.0),
                    right_delta_db=correction_deltas.get("right", 0.0),
                )
                set_audio_output_mode(
                    OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(correction_snapshot),
                    _auto_sub_22_candidate_subwoofers(
                        correction_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                        active_subs=("sub1", "sub2"),
                    ),
                )
                if subwoofer_runtime is not None:
                    await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
                correction_left = await _measure_auto_sub_candidate(
                    delay_ms=best_left, job=job, candidate_index=1, total=2,
                    stage="gain_correction_after", fc=fc, input_id=input_id, channel="left",
                    mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
                    original_highpass=True, measure_channel="left",
                    output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                    original_config_snapshot=correction_snapshot, sub1_alignment_ms=best_left,
                    sub2_alignment_ms=best_right, active_subs=("sub1", "sub2"),
                )
                correction_right = await _measure_auto_sub_candidate(
                    delay_ms=best_right, job=job, candidate_index=2, total=2,
                    stage="gain_correction_after", fc=fc, input_id=input_id, channel="right",
                    mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
                    original_highpass=True, measure_channel="right",
                    output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                    original_config_snapshot=correction_snapshot, sub1_alignment_ms=best_left,
                    sub2_alignment_ms=best_right, active_subs=("sub1", "sub2"),
                )
                correction_after = _calculate_auto_sub_gain(
                    mode=OUTPUT_MODE_SUBWOOFER_22_STEREO, target_curve=job.get("target_curve"),
                    anchor=job.get("main_target_anchor"), winner_curves={
                        "left": correction_left.get("calibrated_points") or [],
                        "right": correction_right.get("calibrated_points") or [],
                    }, crossover_hz=fc,
                )
                if stereo_probe_plan and stereo_probe_plan.get("available"):
                    probe_channels: dict[str, Any] = {}
                    accepted_probe_sides: dict[str, bool] = {}
                    correction_points = {
                        "left": correction_left.get("calibrated_points") or [],
                        "right": correction_right.get("calibrated_points") or [],
                    }
                    for side in ("left", "right"):
                        planned = side in correction_deltas
                        before_corridor = (((stereo_probe_plan.get("channels") or {}).get(side) or {}).get("corridor_before") or {})
                        after_corridor = _auto_sub_stereo_corridor_violation(
                            points=correction_points[side], target_curve=job.get("target_curve"),
                            anchor=job.get("main_target_anchor"), crossover_hz=fc,
                            direction=correction_deltas.get(side, 0.0),
                        ) if planned else before_corridor
                        before_score = abs(float(gain_after["channels"][side]["target_delta_db"]))
                        after_score = abs(float(correction_after["channels"][side]["target_delta_db"]))
                        score_better = after_score < before_score
                        corridor_better = (
                            after_corridor.get("available") is True
                            and float(after_corridor.get("severity_db", 0.0)) < float(before_corridor.get("severity_db", 0.0))
                        )
                        accepted_probe_sides[side] = bool(planned and score_better and corridor_better)
                        probe_channels[side] = {
                            "planned": planned, "accepted": accepted_probe_sides[side],
                            "score_before": round(before_score, 3), "score_after": round(after_score, 3),
                            "score_better": score_better, "corridor_before": before_corridor,
                            "corridor_after": after_corridor, "corridor_better": corridor_better,
                        }
                    accepted_any_probe = any(accepted_probe_sides.values())
                    final_gain_deltas = {
                        side: gain_deltas.get(side, 0.0) + (
                            correction_deltas.get(side, 0.0) if accepted_probe_sides[side] else 0.0
                        ) for side in ("left", "right")
                    }
                    final_gain_snapshot = _auto_sub_22_snapshot_with_gain(
                        polarity_snapshot, left_delta_db=final_gain_deltas["left"],
                        right_delta_db=final_gain_deltas["right"],
                    )
                    final_gain_left = correction_left if accepted_probe_sides["left"] else gain_after_left
                    final_gain_right = correction_right if accepted_probe_sides["right"] else gain_after_right
                    correction_verdict = {
                        "accepted": accepted_any_probe,
                        "reason": "Stereo corridor probe improved score and 1/3-octave violation" if accepted_any_probe else "Stereo corridor probe rejected; Step 1 retained",
                        "channels": probe_channels, "step1_retained": True, "stereo_probe": True,
                    }
                    if not all(accepted_probe_sides.get(side, False) for side in correction_deltas):
                        set_audio_output_mode(
                            OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(final_gain_snapshot),
                            _auto_sub_22_candidate_subwoofers(
                                final_gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                                active_subs=("sub1", "sub2"),
                            ),
                        )
                        if subwoofer_runtime is not None:
                            await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
                else:
                    correction_verdict = _auto_sub_gain_verdict(
                        gain_after, correction_after, OUTPUT_MODE_SUBWOOFER_22_STEREO,
                    )
                    if correction_verdict["accepted"]:
                        final_gain_deltas = {
                            side: gain_deltas.get(side, 0.0) + correction_deltas.get(side, 0.0)
                            for side in ("left", "right")
                        }
                        final_gain_snapshot = correction_snapshot
                        final_gain_left, final_gain_right = correction_left, correction_right
                    else:
                        set_audio_output_mode(
                            OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(gain_snapshot),
                            _auto_sub_22_candidate_subwoofers(
                                gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                                active_subs=("sub1", "sub2"),
                            ),
                        )
                        if subwoofer_runtime is not None:
                            await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        _auto_sub_gain_log_line("AUTOGAIN_FEEDBACK", {
            "gain_after_step1": {
                "left": float(_auto_sub_22_sub(gain_snapshot, "sub1").get("level_db", 0.0)),
                "right": float(_auto_sub_22_sub(gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "score_before": _auto_sub_gain_log_score(job["auto_gain"]),
            "score_after_step1": _auto_sub_gain_log_score(gain_after),
            "response_per_db_left": (((correction_plan or {}).get("channels") or {}).get("left") or {}).get("response_change_per_db"),
            "response_per_db_right": (((correction_plan or {}).get("channels") or {}).get("right") or {}).get("response_change_per_db"),
            "remaining_error_left": (gain_after.get("channels", {}).get("left") or {}).get("target_delta_db"),
            "remaining_error_right": (gain_after.get("channels", {}).get("right") or {}).get("target_delta_db"),
            "raw_correction_db": (correction_plan or {}).get("raw_deltas_db") or None,
            "applied_correction_db": (correction_plan or {}).get("applied_deltas_db") or None,
            "correction_step_db": correction_deltas or None,
        })
        decision = "accepted_step2" if correction_verdict and correction_verdict.get("accepted") else (
            "accepted_step1" if step1_retained else "restored"
        )
        if decision == "accepted_step2":
            if correction_verdict.get("stereo_probe"):
                score_final_source = copy.deepcopy(gain_after)
                for side in ("left", "right"):
                    if ((correction_verdict.get("channels") or {}).get(side) or {}).get("accepted"):
                        score_final_source["channels"][side] = copy.deepcopy(correction_after["channels"][side])
            else:
                score_final_source = correction_after
        elif decision == "accepted_step1":
            score_final_source = copy.deepcopy(job["auto_gain"])
            for side in ("left", "right"):
                if accepted_step1_sides[side]:
                    score_final_source["channels"][side] = copy.deepcopy(gain_after["channels"][side])
        else:
            score_final_source = job["auto_gain"]
        _auto_sub_gain_log_line("AUTOGAIN_RESULT", {
            "gain_final": {
                "left": float(_auto_sub_22_sub(final_gain_snapshot, "sub1").get("level_db", 0.0)),
                "right": float(_auto_sub_22_sub(final_gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "score_final": _auto_sub_gain_log_score(score_final_source), "decision": decision,
            "reason": ((correction_verdict or gain_verdict) or {}).get("reason"),
            "delay_final": {"left_ms": best_left, "right_ms": best_right},
        })
        job["auto_gain"].update({
            "applied": bool(step1_retained and gain_deltas),
            "reverted": bool(gain_deltas and not step1_retained),
            "accepted_step1_sides": accepted_step1_sides,
            "verification": gain_after, "verification_verdict": gain_verdict,
            "response_correction": correction_plan,
            "stereo_corridor_probe": stereo_probe_plan,
            "correction_deltas_db": correction_deltas,
            "correction_verification": correction_after,
            "correction_verdict": correction_verdict,
            "final_deltas_db": final_gain_deltas,
            "original_levels_db": {
                "sub1": float(original_left.get("level_db", 0.0)), "sub2": float(original_right.get("level_db", 0.0)),
            },
            "final_levels_db": {
                "sub1": float(_auto_sub_22_sub(final_gain_snapshot, "sub1").get("level_db", 0.0)),
                "sub2": float(_auto_sub_22_sub(final_gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "stage_output_peaks": (final_gain_sweep or {}).get("stage_output_peaks"),
        })
        overall_score = (0.6 * min(left_score, right_score)) + (0.4 * ((left_score + right_score) / 2.0))
        left_xo_score = float(left_winner.get("xo_score", 0.0) or 0.0)
        right_xo_score = float(right_winner.get("xo_score", 0.0) or 0.0)
        left_timing_score = float(left_winner.get("timing_band_score", 0.0) or 0.0)
        right_timing_score = float(right_winner.get("timing_band_score", 0.0) or 0.0)
        left_low_guard_loss = float(left_winner.get("low_guard_loss_db", 0.0) or 0.0)
        right_low_guard_loss = float(right_winner.get("low_guard_loss_db", 0.0) or 0.0)
        left_low_guard_penalty = float(left_winner.get("low_guard_penalty", 0.0) or 0.0)
        right_low_guard_penalty = float(right_winner.get("low_guard_penalty", 0.0) or 0.0)
        overall_low_guard_loss = max(left_low_guard_loss, right_low_guard_loss)
        overall_low_guard_penalty = (
            0.6 * max(left_low_guard_penalty, right_low_guard_penalty)
            + 0.4 * ((left_low_guard_penalty + right_low_guard_penalty) / 2.0)
        )
        left_score_pct = round(left_score * 100.0, 1)
        right_score_pct = round(right_score * 100.0, 1)
        overall_score_pct = round(overall_score * 100.0, 1)
        job["status"] = "completed"
        job["message"] = (
            f"Applied 2.2 Stereo Bass: Left Sub {best_left:.2f} ms / "
            f"Right Sub {best_right:.2f} ms (overall {overall_score_pct:.1f} %)"
        )
        _log_auto_sub_timing_summary(job)

        # Build baseline and confirmation measurements for before/after graph display
        all_left_sweeps = list(left_results) + list(left_fine_results)
        all_right_sweeps = list(right_results) + list(right_fine_results)
        left_baseline = _auto_sub_result_for_delay(all_left_sweeps, original_left_alignment)
        right_baseline = _auto_sub_result_for_delay(all_right_sweeps, original_right_alignment)
        left_confirm = final_gain_left if accepted_step1_sides["left"] else _auto_sub_result_for_delay(all_left_sweeps, best_left)
        right_confirm = final_gain_right if accepted_step1_sides["right"] else _auto_sub_result_for_delay(all_right_sweeps, best_right)

        # One shared vertical offset from baseline L+R bass region so that
        # Before/After and L/R relative level differences are preserved.
        _stereo_offset_db = _auto_sub_shared_bass_offset(
            left_baseline.get("points") if left_baseline else [],
            right_baseline.get("points") if right_baseline else [],
        )

        def _stereo_measurement_from_lr(left_sweep, right_sweep, label, name, offset_db):
            traces = []
            base_id = uuid4().hex[:12]
            if left_sweep:
                pts = left_sweep.get("points") or []
                if isinstance(pts, list) and len(pts) >= 3:
                    points = [[float(p[0]), float(p[1]) - offset_db] for p in pts]
                    traces.append({"kind": "measured", "label": f"{label} L", "role": "left", "points": points})
            if right_sweep:
                pts = right_sweep.get("points") or []
                if isinstance(pts, list) and len(pts) >= 3:
                    points = [[float(p[0]), float(p[1]) - offset_db] for p in pts]
                    traces.append({"kind": "measured", "label": f"{label} R", "role": "right", "points": points})
            return {"id": f"autosub-{base_id}", "name": name, "traces": traces} if traces else None

        baseline_measurement = _stereo_measurement_from_lr(
            left_baseline, right_baseline, "Before",
            f"AutoSub 2.2S Baseline (L {original_left_alignment:.1f} / R {original_right_alignment:.1f} ms)",
            _stereo_offset_db,
        )
        confirmation_measurement = _stereo_measurement_from_lr(
            left_confirm, right_confirm, "After",
            f"AutoSub 2.2S Optimized (L {best_left:.1f} / R {best_right:.1f} ms)",
            _stereo_offset_db,
        )

        job["result"] = {
            "mode": OUTPUT_MODE_SUBWOOFER_22_STEREO,
            "original_sub1_alignment_ms": original_left_alignment,
            "original_sub2_alignment_ms": original_right_alignment,
            "suggested_sub1_alignment_ms": best_left,
            "suggested_sub2_alignment_ms": best_right,
            "applied_sub1_alignment_ms": best_left,
            "applied_sub2_alignment_ms": best_right,
            "applied": True,
            "auto_applied": True,
            "apply_decision": "applied_22_stereo_separate_lr",
            "candidate_ledger": candidate_ledger,
            "crossover_hz": fc,
            "confidence": "left_right_separate",
            "winner": {
                "name": _auto_sub_22_stereo_name(best_left, best_right),
                "score": round(overall_score, 4),
                "score_pct": overall_score_pct,
                "overall_score": round(overall_score, 4),
                "overall_score_pct": overall_score_pct,
                "xo_score": round((left_xo_score + right_xo_score) / 2.0, 4),
                "timing_band_score": round((left_timing_score + right_timing_score) / 2.0, 4),
                "low_guard_loss_db": round(overall_low_guard_loss, 2),
                "low_guard_penalty": round(overall_low_guard_penalty, 4),
                "final_score": round(overall_score, 4),
                "low_guard_loss_L_db": round(left_low_guard_loss, 2),
                "low_guard_loss_R_db": round(right_low_guard_loss, 2),
                "low_guard_penalty_L": round(left_low_guard_penalty, 4),
                "low_guard_penalty_R": round(right_low_guard_penalty, 4),
                "score_L_pct": left_score_pct,
                "score_R_pct": right_score_pct,
            },
            "left_score": round(left_score, 4),
            "right_score": round(right_score, 4),
            "overall_score": round(overall_score, 4),
            "xo_score": round((left_xo_score + right_xo_score) / 2.0, 4),
            "timing_band_score": round((left_timing_score + right_timing_score) / 2.0, 4),
            "low_guard_loss_db": round(overall_low_guard_loss, 2),
            "low_guard_penalty": round(overall_low_guard_penalty, 4),
            "final_score": round(overall_score, 4),
            "low_guard_loss_L_db": round(left_low_guard_loss, 2),
            "low_guard_loss_R_db": round(right_low_guard_loss, 2),
            "low_guard_penalty_L": round(left_low_guard_penalty, 4),
            "low_guard_penalty_R": round(right_low_guard_penalty, 4),
            "left_score_pct": left_score_pct,
            "right_score_pct": right_score_pct,
            "overall_score_pct": overall_score_pct,
            "accepted_winner": {
                "name": _auto_sub_22_stereo_name(best_left, best_right),
                "left_winner": left_winner,
                "right_winner": right_winner,
                "score": round(overall_score, 4),
                "score_pct": overall_score_pct,
            },
            "fine_accepted": bool(left_acceptance["fine_accepted"] or right_acceptance["fine_accepted"]),
            "reject_reason": {
                "left": left_acceptance["reject_reason"],
                "right": right_acceptance["reject_reason"],
            },
            "left_coarse_winner": left_coarse_winner,
            "left_fine_winner": left_fine_winner,
            "right_coarse_winner": right_coarse_winner,
            "right_fine_winner": right_fine_winner,
            "left_winner": left_winner,
            "right_winner": right_winner,
            "left_incumbent_winner": left_incumbent_winner,
            "right_incumbent_winner": right_incumbent_winner,
            "left_incumbent_score": left_acceptance["incumbent_score"],
            "right_incumbent_score": right_acceptance["incumbent_score"],
            "left_accepted_winner": left_winner,
            "right_accepted_winner": right_winner,
            "left_fine_accepted": left_acceptance["fine_accepted"],
            "right_fine_accepted": right_acceptance["fine_accepted"],
            "left_reject_reason": left_acceptance["reject_reason"],
            "right_reject_reason": right_acceptance["reject_reason"],
            "left_coarse_ranking": left_coarse_scoring["results"],
            "left_fine_ranking": left_fine_scoring["results"] if left_fine_scoring else [],
            "right_coarse_ranking": right_coarse_scoring["results"],
            "right_fine_ranking": right_fine_scoring["results"] if right_fine_scoring else [],
            "left_ranking": left_scoring["results"],
            "right_ranking": right_scoring["results"],
            "fine_scan": job["fine_scan"],
            "sweep_count": actual_sweep_total + 2,
            "candidate_count": actual_sweep_total,
            "left_candidate_count": len(left_scan_delays) + len(left_fine_delays),
            "right_candidate_count": len(right_scan_delays) + len(right_fine_delays),
            "left_coarse_candidate_count": len(left_scan_delays),
            "left_fine_candidate_count": len(left_fine_delays),
            "right_coarse_candidate_count": len(right_scan_delays),
            "right_fine_candidate_count": len(right_fine_delays),
            "valid_count": len(left_final_valid) + len(right_final_valid),
            "left_valid_count": len(left_final_valid),
            "right_valid_count": len(right_final_valid),
            "left_coarse_valid_count": len(left_valid),
            "left_fine_valid_count": len(left_fine_valid),
            "right_coarse_valid_count": len(right_valid),
            "right_fine_valid_count": len(right_fine_valid),
            "baseline_measurement": baseline_measurement,
            "confirmation_measurement": confirmation_measurement,
            **derived_delays,
        }
        logger.info(
            "Auto-sub 2.2 Stereo Bass optimize completed: fc=%sHz left %.2f->%.2fms right %.2f->%.2fms "
            "overall_score=%.1f%% score_L=%.1f%% score_R=%.1f%%",
            fc,
            original_left_alignment,
            best_left,
            original_right_alignment,
            best_right,
            overall_score_pct,
            left_score_pct,
            right_score_pct,
        )

    except Exception as exc:
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        logger.exception("Auto-sub 2.2 Stereo Bass optimize failed")
        job["status"] = "failed"
        job["message"] = f"Auto Sub Optimize 2.2 Stereo Bass failed: {exc}"
        job["error"] = {"detail": str(exc)}
        await _restore_original_config()

    finally:
        if measurement_sr_session is not None:
            await measurement_sr_session.unregister_auto_sub(job_id)
        _finalize_autosub_job(job, job_id)
        try:
            _auto_sub_lock.release()
        except RuntimeError:
            pass
        cleanup_job_id = job_id

        async def _cleanup_autosub_job():
            await asyncio.sleep(600)
            _AUTO_SUB_JOBS.pop(cleanup_job_id, None)

        asyncio.create_task(_cleanup_autosub_job())


async def _run_auto_sub_optimize(
    job_id: str,
    input_id: str,
    channel: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    scan_delays: list[float],
    fc: int,
    current_alignment: float,
    original_polarity: str,
    original_level: float,
    original_highpass: bool,
    original_config_snapshot: dict[str, Any],
) -> None:
    global measurement_store, subwoofer_runtime, _auto_sub_lock
    from samplerate import _load_audio_output_mode, set_audio_output_mode

    job = _AUTO_SUB_JOBS.get(job_id)
    if not job:
        _auto_sub_lock.release()
        return

    async def _restore_original_config():
        """Restore subwoofer config from snapshot."""
        await _restore_auto_sub_original_config(original_config_snapshot)

    if measurement_sr_session is not None:
        await measurement_sr_session.register_auto_sub(job_id)
    try:
        if _auto_sub_cancel_requested(job):
            logger.info("AUTOSUB job=%s cancel observed (before sweeps)", job_id)
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        sweep_results: list[dict[str, Any]] = []
        total = len(scan_delays) * 2

        # AutoSub bass-focused sweep settings
        auto_sub_sweep_low_hz = 20.0
        auto_sub_sweep_high_hz = max(600.0, min(float(fc) * 8.0, 2000.0))
        if fc <= 60:
            auto_sub_sweep_sec, auto_sub_tail_sec = 3.5, 1.5
        elif fc <= 120:
            auto_sub_sweep_sec, auto_sub_tail_sec = 3.0, 1.3
        else:
            auto_sub_sweep_sec, auto_sub_tail_sec = 2.5, 1.1
        auto_sub_sweep_profile = {
            "sweep_start_hz": auto_sub_sweep_low_hz,
            "sweep_end_hz": auto_sub_sweep_high_hz,
            "sweep_seconds": auto_sub_sweep_sec,
            "tail_seconds": auto_sub_tail_sec,
        }

        # Resolve sample rate once for all sweeps
        auto_sub_rate = _resolve_measurement_start_sample_rate()
        await _capture_auto_sub_main_references(
            job=job, fc=fc, input_id=input_id,
            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
            auto_sub_rate=auto_sub_rate, output_mode=OUTPUT_MODE_SUBWOOFER_21,
            original_config_snapshot=original_config_snapshot,
        )
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        coarse_total = len(scan_delays)
        coarse_sweep_total = coarse_total * 2
        total = coarse_sweep_total
        for idx, delay_ms in enumerate(scan_delays):
            sweep_results.append(
                await _measure_auto_sub_combined_candidate(
                    delay_ms=delay_ms,
                    job=job,
                    candidate_index=idx + 1,
                    total=coarse_total,
                    sweep_index_start=(idx * 2) + 1,
                    sweep_total=coarse_sweep_total,
                    stage="coarse",
                    fc=fc,
                    input_id=input_id,
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate,
                    original_level=original_level,
                    original_polarity=original_polarity,
                    original_highpass=original_highpass,
                )
            )
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

            # Live: push baseline measurement to job for frontend polling display
            if round(float(delay_ms), 2) == round(float(current_alignment), 2):
                last_result = sweep_results[-1]
                if _auto_sub_has_points(last_result, "points_left") or _auto_sub_has_points(last_result, "points_right"):
                    job["baseline_measurement"] = _auto_sub_measurement_from_sweep(
                        last_result, "Before", f"AutoSub Baseline ({current_alignment:.1f} ms)"
                    )

        # Score candidates
        valid = [r for r in sweep_results if _auto_sub_has_points(r, "points_left") or _auto_sub_has_points(r, "points_right")]
        if not valid:
            job["status"] = "failed"
            job["message"] = "No valid sweep results to score"
            job["error"] = {"detail": "All sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return

        step_ms = _auto_sub_step_ms(fc)
        coarse_scoring = _score_auto_sub_combined_candidates(
            sweep_results,
            crossover_hz=fc,
            low_guard_reference_delay_ms=current_alignment,
        )
        valid = list(coarse_scoring.get("scored_candidates") or valid)
        coarse_winner = coarse_scoring["winner"]
        coarse_runner_up = coarse_scoring.get("runner_up")
        fine_trigger_reasons = _auto_sub_fine_trigger_reasons(coarse_scoring, scan_delays)
        fine_delays: list[float] = []
        fine_results: list[dict[str, Any]] = []
        fine_valid: list[dict[str, Any]] = []
        fine_winner: dict[str, Any] | None = None
        fine_scoring: dict[str, Any] | None = None

        fine_scan: dict[str, Any] = {
            "enabled": bool(fine_trigger_reasons),
            "triggered": False,
            "reasons": fine_trigger_reasons,
            "step_ms": step_ms,
            "fine_step_ms": step_ms / 4.0,
            "candidates": [],
            "sweep_count": 0,
            "valid_count": 0,
            "status": "skipped" if not fine_trigger_reasons else "pending",
            "coarse_winner": coarse_winner,
            "coarse_runner_up": coarse_runner_up,
        }
        final_decision_pool = list(sweep_results)

        if fine_trigger_reasons:
            fine_delays = _auto_sub_fine_delay_candidates(coarse_winner, coarse_runner_up, step_ms, {round(float(delay), 2) for delay in scan_delays})
            fine_scan.update({
                "triggered": True,
                "candidates": fine_delays,
                "status": "running" if fine_delays else "skipped",
            })
            job["fine_scan"] = fine_scan
            if fine_delays:
                fine_candidate_total = len(fine_delays)
                fine_sweep_total = fine_candidate_total * 2
                total = coarse_sweep_total + fine_sweep_total
                reason_text = ", ".join(fine_trigger_reasons)
                job["stage"] = "fine_scan"
                job["message"] = f"Fine-Scan triggered ({reason_text}); {len(fine_delays)} candidates"
                job["progress"] = {
                    "current": coarse_sweep_total,
                    "total": total,
                    "sweep_current": coarse_sweep_total,
                    "sweep_total": total,
                    "candidate_current": 0,
                    "candidate_total": fine_candidate_total,
                    "stage": "fine",
                    "reason": reason_text,
                }
                if _auto_sub_cancel_requested(job):
                    job["message"] = "Auto Sub Optimize cancelled."
                    await _restore_original_config()
                    return
                for idx, delay_ms in enumerate(fine_delays):
                    fine_results.append(
                        await _measure_auto_sub_combined_candidate(
                            delay_ms=delay_ms,
                            job=job,
                            candidate_index=idx + 1,
                            total=fine_candidate_total,
                            sweep_index_start=coarse_sweep_total + (idx * 2) + 1,
                            sweep_total=total,
                            stage="fine",
                            fc=fc,
                            input_id=input_id,
                            mic_input_channel=mic_input_channel,
                            reference_input_channel=reference_input_channel,
                            calibration_ref=calibration_ref,
                            calibration_filename=calibration_filename,
                            calibration_bytes=calibration_bytes,
                            auto_sub_sweep_profile=auto_sub_sweep_profile,
                            auto_sub_rate=auto_sub_rate,
                            original_level=original_level,
                            original_polarity=original_polarity,
                            original_highpass=original_highpass,
                        )
                    )
                    if _auto_sub_cancel_requested(job):
                        job["message"] = "Auto Sub Optimize cancelled."
                        await _restore_original_config()
                        return

                fine_valid = [r for r in fine_results if _auto_sub_has_points(r, "points_left") or _auto_sub_has_points(r, "points_right")]
                if fine_valid:
                    fine_scoring = _score_auto_sub_combined_candidates(
                        fine_results,
                        crossover_hz=fc,
                        low_guard_reference_delay_ms=current_alignment,
                    )
                    fine_valid = list(fine_scoring.get("scored_candidates") or fine_valid)
                    fine_winner = fine_scoring["winner"]
                    fine_scan.update({
                        "status": "completed",
                        "candidate_count": len(fine_delays),
                        "sweep_count": len(fine_delays) * 2,
                        "valid_count": len(fine_valid),
                        "winner": fine_winner,
                        "runner_up": fine_scoring.get("runner_up"),
                        "results": fine_scoring["results"],
                    })
                    combined_valid = valid + fine_valid
                    final_decision_pool = list(combined_valid)
                    final_scoring = _score_auto_sub_combined_candidates(
                        combined_valid,
                        crossover_hz=fc,
                        low_guard_reference_delay_ms=current_alignment,
                    )
                    combined_valid = list(final_scoring.get("scored_candidates") or combined_valid)
                else:
                    fine_scan.update({
                        "status": "no_valid_results",
                        "candidate_count": len(fine_delays),
                        "sweep_count": len(fine_delays) * 2,
                        "valid_count": 0,
                        "winner": None,
                        "runner_up": None,
                        "results": fine_results,
                    })
                    combined_valid = valid
                    final_scoring = coarse_scoring
            else:
                fine_scan.update({
                    "status": "skipped",
                    "reason": "no fine candidates generated",
                })
                combined_valid = valid
                final_scoring = coarse_scoring
        else:
            combined_valid = valid
            final_scoring = coarse_scoring

        job["fine_scan"] = fine_scan
        _auto_sub_rank_results(final_scoring["results"])

        # Re-attach scan stage from original measured candidates (scoring creates fresh dicts)
        scan_by_delay: dict[float, str] = {}
        for result in valid:
            delay_key = round(float(result.get("delay_ms", 0.0)), 2)
            scan_by_delay[delay_key] = result.get("scan", "coarse")
        for result in fine_valid:
            delay_key = round(float(result.get("delay_ms", 0.0)), 2)
            scan_by_delay[delay_key] = result.get("scan", "fine")

        coarse_score_by_delay = {
            round(float(result.get("delay_ms", 0.0)), 2): result
            for result in coarse_scoring["results"]
        }
        for result in final_scoring["results"]:
            delay_key = round(float(result.get("delay_ms", 0.0)), 2)
            result["scan"] = scan_by_delay.get(delay_key, "coarse")
            if result["scan"] == "coarse":
                coarse_score = coarse_score_by_delay.get(delay_key)
                if coarse_score:
                    result["coarse_score"] = coarse_score.get("score")
                    result["coarse_score_pct"] = coarse_score.get("score_pct")
                    result["coarse_rank"] = coarse_score.get("rank")

        final_fine_winner = next(
            (result for result in final_scoring["results"] if result.get("scan") == "fine"),
            fine_winner,
        )
        if final_fine_winner is not None and fine_scan.get("status") == "completed":
            fine_scan["final_winner"] = final_fine_winner
        final_coarse_winner = _auto_sub_best_scan_result(final_scoring["results"], "coarse") or coarse_winner
        incumbent_winner = _auto_sub_result_for_delay(final_scoring["results"], current_alignment)
        acceptance = _auto_sub_select_accepted_winner(
            coarse_winner=final_coarse_winner,
            fine_winner=final_fine_winner if fine_scan.get("status") == "completed" else None,
            incumbent_winner=incumbent_winner,
        )
        fine_scan["coarse_winner"] = final_coarse_winner
        fine_scan["fine_winner"] = final_fine_winner
        fine_scan["incumbent_winner"] = incumbent_winner
        fine_scan["incumbent_score"] = acceptance["incumbent_score"]
        fine_scan["accepted_winner"] = acceptance["accepted_winner"]
        fine_scan["fine_accepted"] = acceptance["fine_accepted"]
        fine_scan["reject_reason"] = acceptance["reject_reason"]

        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        winner = acceptance["accepted_winner"]
        best_delay = winner["delay_ms"]
        confidence = str(final_scoring.get("confidence") or "uncertain")
        runner_up = final_scoring.get("runner_up")
        winner_score_pct = float(winner.get("score_pct", 0.0) or 0.0)
        runner_score_pct = float(runner_up.get("score_pct", 0.0) or 0.0) if runner_up else 0.0
        winner_margin_pct = winner_score_pct - runner_score_pct if runner_up else 100.0
        original_score_pct = None
        original_delay_key = round(float(current_alignment), 2)
        for scored_result in final_scoring.get("results", []):
            if round(float(scored_result.get("delay_ms", 0.0)), 2) == original_delay_key:
                original_score_pct = float(scored_result.get("score_pct", 0.0) or 0.0)
                break
        score_gain_pct = winner_score_pct - original_score_pct if original_score_pct is not None else None

        auto_apply = False
        apply_decision = "not_applied_uncertain_confidence"
        if incumbent_winner is not None and round(float(best_delay), 2) == round(float(current_alignment), 2):
            apply_decision = "not_applied_incumbent_better"
        elif confidence == "clear":
            auto_apply = True
            apply_decision = "applied_clear_confidence"
        elif confidence == "close":
            if winner_margin_pct < 2.0:
                apply_decision = "not_applied_close_margin_below_2pp"
            elif score_gain_pct is not None and score_gain_pct < 3.0:
                apply_decision = "not_applied_close_gain_below_3pp"
            else:
                auto_apply = True
                apply_decision = "applied_close_confidence"
        elif (
            confidence == "uncertain"
            and score_gain_pct is not None
            and score_gain_pct >= 10.0
            and winner_score_pct >= 70.0
        ):
            auto_apply = True
            apply_decision = "applied_uncertain_large_gain"

        apply_ok = False
        applied_delay = current_alignment
        if auto_apply:
            try:
                sub_config = {
                    "crossover_frequency_hz": fc,
                    "sub_alignment_ms": best_delay,
                    "sub_level_db": original_level,
                    "sub_polarity": original_polarity,
                    "main_highpass_enabled": original_highpass,
                }
                set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, sub_config)
                if subwoofer_runtime is not None:
                    config = SubwooferRuntimeConfig.from_overview(get_audio_output_overview())
                    await subwoofer_runtime.sync(config)
                await asyncio.sleep(0.3)
                verify = _load_audio_output_mode()
                if float(verify.get("subwoofer", {}).get("sub_alignment_ms", -999)) == best_delay:
                    apply_ok = True
                    applied_delay = best_delay
            except Exception as exc:
                logger.exception("Auto-sub: failed to apply winner delay %.2f ms", best_delay)
        else:
            await _restore_original_config()
            apply_ok = True

        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        if not apply_ok:
            job["status"] = "failed"
            job["message"] = f"Scoring succeeded but failed to apply winner delay {best_delay} ms"
            job["error"] = {"detail": "Winner apply failed — original config restored"}
            await _restore_original_config()
            return

        stored_winner = winner if auto_apply else (incumbent_winner or winner)
        gain_winner = _auto_sub_result_for_delay(list(sweep_results) + list(fine_results), stored_winner.get("delay_ms", current_alignment)) or {}
        final_polarity = original_polarity
        polarity_check: dict[str, Any] = {
            "incumbent": original_polarity, "selected": original_polarity,
            "accepted": False, "reason": "incumbent_protected",
        }
        if gain_winner and auto_apply:
            opposite = _auto_sub_opposite_polarity(original_polarity)
            alt = await _measure_auto_sub_combined_candidate(
                delay_ms=applied_delay, job=job, candidate_index=1, total=1,
                sweep_index_start=total + 1, sweep_total=total + 2, stage="polarity_check", fc=fc,
                input_id=input_id, mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                original_level=original_level, original_polarity=opposite,
                original_highpass=original_highpass,
            )
            try:
                polarity_scoring = _score_auto_sub_combined_candidates(
                    [dict(gain_winner, delay_ms=0.0), dict(alt, delay_ms=1.0)], crossover_hz=fc,
                    low_guard_reference_delay_ms=0.0,
                )
                scored_incumbent = _auto_sub_result_for_delay(polarity_scoring["results"], 0.0) or {}
                scored_alt = _auto_sub_result_for_delay(polarity_scoring["results"], 1.0) or {}
                polarity_check.update(_auto_sub_polarity_decision(scored_incumbent, scored_alt))
                polarity_check.update({"alternative": opposite, "incumbent_score": scored_incumbent.get("score"), "alternative_score": scored_alt.get("score")})
                if polarity_check["accepted"]:
                    final_polarity = opposite
                    gain_winner = alt
                    polarity_check["selected"] = opposite
                    local_delays = [
                        _auto_sub_clamped_delay(applied_delay + offset)
                        for offset in (-_auto_sub_step_ms(fc) / 2.0, -_auto_sub_step_ms(fc) / 4.0,
                                       _auto_sub_step_ms(fc) / 4.0, _auto_sub_step_ms(fc) / 2.0)
                    ]
                    polarity_fine: list[dict[str, Any]] = [alt]
                    for idx, delay in enumerate(local_delays):
                        polarity_fine.append(await _measure_auto_sub_combined_candidate(
                            delay_ms=delay, job=job, candidate_index=idx + 1, total=len(local_delays),
                            sweep_index_start=total + 3 + idx * 2, sweep_total=total + 2 + len(local_delays) * 2,
                            stage="polarity_fine", fc=fc, input_id=input_id,
                            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                            auto_sub_rate=auto_sub_rate, original_level=original_level,
                            original_polarity=opposite, original_highpass=original_highpass,
                        ))
                    fine_scored = _score_auto_sub_combined_candidates(polarity_fine, crossover_hz=fc)
                    fine_best = fine_scored["winner"]
                    fine_measured = _auto_sub_result_for_delay(polarity_fine, float(fine_best.get("delay_ms", applied_delay)))
                    if fine_measured:
                        applied_delay = float(fine_best["delay_ms"])
                        gain_winner = fine_measured
                    polarity_check["fine_scan"] = {"candidates": local_delays, "winner": fine_best}
            except Exception as exc:
                logger.warning("Auto-sub polarity check unavailable; restoring incumbent: %s", exc)
                final_polarity = original_polarity
                polarity_check.update({"accepted": False, "selected": original_polarity, "reason": "measurement_or_scoring_failed"})

            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, {
                "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                "sub_level_db": original_level, "sub_polarity": final_polarity,
                "main_highpass_enabled": original_highpass,
            })
            if subwoofer_runtime is not None:
                await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        job["polarity_check"] = polarity_check
        job["auto_gain"] = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_21,
            target_curve=job.get("target_curve"), anchor=job.get("main_target_anchor"),
            winner_curves={
                "left": gain_winner.get("calibrated_points_left") or [],
                "right": gain_winner.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        logger.info("AUTOSUB_GAIN mode=2.1 diagnostics=%s", json.dumps(job["auto_gain"], sort_keys=True))
        gain_deltas = _auto_sub_gain_deltas(job["auto_gain"], OUTPUT_MODE_SUBWOOFER_21, max_abs_db=6.0)
        applied_gain_delta = gain_deltas.get("left", 0.0)
        _auto_sub_gain_log_line("AUTOGAIN_INIT", {
            "mode": OUTPUT_MODE_SUBWOOFER_21, "xo_hz": fc,
            "target": (job.get("target_curve") or {}).get("label"),
            "anchor_hz": (job.get("main_target_anchor") or {}).get("usable_band_hz"),
            "target_offset_db": (job.get("main_target_anchor") or {}).get("target_vertical_offset_db"),
            "gain_before": original_level,
            "winner_delta_left": (job["auto_gain"].get("channels", {}).get("left") or {}).get("target_delta_db"),
            "winner_delta_right": (job["auto_gain"].get("channels", {}).get("right") or {}).get("target_delta_db"),
            "combined_delta_db": (job["auto_gain"].get("recommendation") or {}).get("raw_delta_db"),
            "first_step_db": applied_gain_delta,
        })
        gained_level = max(-24.0, min(12.0, original_level + applied_gain_delta))
        if gain_deltas:
            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, {
                "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                "sub_level_db": gained_level, "sub_polarity": final_polarity,
                "main_highpass_enabled": original_highpass,
            })
            if subwoofer_runtime is not None:
                await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        gain_after_sweep = await _measure_auto_sub_combined_candidate(
            delay_ms=applied_delay, job=job, candidate_index=1, total=1,
            sweep_index_start=total + 1, sweep_total=total + 2, stage="gain_after", fc=fc,
            input_id=input_id, mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
            calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
            auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
            original_level=gained_level, original_polarity=final_polarity,
            original_highpass=original_highpass, output_mode=OUTPUT_MODE_SUBWOOFER_21,
            original_config_snapshot=original_config_snapshot,
        )
        gain_after = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_21, target_curve=job.get("target_curve"),
            anchor=job.get("main_target_anchor"), winner_curves={
                "left": gain_after_sweep.get("calibrated_points_left") or [],
                "right": gain_after_sweep.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        gain_verdict = _auto_sub_gain_verdict(job["auto_gain"], gain_after, OUTPUT_MODE_SUBWOOFER_21)
        final_gain_deltas = gain_deltas if gain_verdict["accepted"] else {"left": 0.0, "right": 0.0}
        final_gain_level = gained_level if gain_verdict["accepted"] else original_level
        final_gain_sweep = gain_after_sweep if gain_verdict["accepted"] else gain_winner
        correction_deltas: dict[str, float] = {}
        correction_plan = None
        correction_after = None
        correction_verdict = None
        if not gain_verdict["accepted"]:
            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, {
                "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                "sub_level_db": original_level, "sub_polarity": final_polarity,
                "main_highpass_enabled": original_highpass,
            })
            if subwoofer_runtime is not None:
                await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        else:
            correction_plan = _auto_sub_gain_response_correction(
                job["auto_gain"], gain_after, gain_deltas, OUTPUT_MODE_SUBWOOFER_21,
            )
            correction_deltas = correction_plan.get("deltas_db") or {}
            correction_delta = correction_deltas.get("left", 0.0)
            corrected_level = max(-24.0, min(12.0, gained_level + correction_delta))
            if not correction_plan.get("available"):
                correction_verdict = {
                    "accepted": False,
                    "reason": correction_plan.get("reason"),
                    "channels": {},
                    "step1_retained": True,
                }
            elif abs(correction_delta) > 0.0005:
                set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, {
                    "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                    "sub_level_db": corrected_level, "sub_polarity": final_polarity,
                    "main_highpass_enabled": original_highpass,
                })
                if subwoofer_runtime is not None:
                    await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
                correction_sweep = await _measure_auto_sub_combined_candidate(
                    delay_ms=applied_delay, job=job, candidate_index=1, total=1,
                    sweep_index_start=total + 3, sweep_total=total + 4, stage="gain_correction_after", fc=fc,
                    input_id=input_id, mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                    original_level=corrected_level, original_polarity=final_polarity,
                    original_highpass=original_highpass, output_mode=OUTPUT_MODE_SUBWOOFER_21,
                    original_config_snapshot=original_config_snapshot,
                )
                correction_after = _calculate_auto_sub_gain(
                    mode=OUTPUT_MODE_SUBWOOFER_21, target_curve=job.get("target_curve"),
                    anchor=job.get("main_target_anchor"), winner_curves={
                        "left": correction_sweep.get("calibrated_points_left") or [],
                        "right": correction_sweep.get("calibrated_points_right") or [],
                    }, crossover_hz=fc,
                )
                correction_verdict = _auto_sub_gain_verdict(gain_after, correction_after, OUTPUT_MODE_SUBWOOFER_21)
                if correction_verdict["accepted"]:
                    final_gain_deltas = {
                        "left": applied_gain_delta + correction_delta,
                        "right": applied_gain_delta + correction_delta,
                    }
                    final_gain_level = corrected_level
                    final_gain_sweep = correction_sweep
                else:
                    set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, {
                        "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                        "sub_level_db": gained_level, "sub_polarity": final_polarity,
                        "main_highpass_enabled": original_highpass,
                    })
                    if subwoofer_runtime is not None:
                        await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        _auto_sub_gain_log_line("AUTOGAIN_FEEDBACK", {
            "gain_after_step1": gained_level,
            "score_before": _auto_sub_gain_log_score(job["auto_gain"]),
            "score_after_step1": _auto_sub_gain_log_score(gain_after),
            "response_per_db_left": (((correction_plan or {}).get("channels") or {}).get("left") or {}).get("response_change_per_db"),
            "response_per_db_right": (((correction_plan or {}).get("channels") or {}).get("right") or {}).get("response_change_per_db"),
            "remaining_error_left": (gain_after.get("channels", {}).get("left") or {}).get("target_delta_db"),
            "remaining_error_right": (gain_after.get("channels", {}).get("right") or {}).get("target_delta_db"),
            "raw_correction_db": ((correction_plan or {}).get("raw_deltas_db") or {}).get("left"),
            "applied_correction_db": ((correction_plan or {}).get("applied_deltas_db") or {}).get("left"),
            "correction_step_db": correction_deltas.get("left") if correction_deltas else None,
        })
        decision = "accepted_step2" if correction_verdict and correction_verdict.get("accepted") else (
            "accepted_step1" if gain_verdict.get("accepted") else "restored"
        )
        score_final_source = correction_after if decision == "accepted_step2" else (gain_after if decision == "accepted_step1" else job["auto_gain"])
        _auto_sub_gain_log_line("AUTOGAIN_RESULT", {
            "gain_final": final_gain_level, "score_final": _auto_sub_gain_log_score(score_final_source),
            "decision": decision, "reason": ((correction_verdict or gain_verdict) or {}).get("reason"),
            "delay_final": applied_delay,
        })
        job["auto_gain"].update({
            "applied": bool(gain_verdict["accepted"] and gain_deltas),
            "reverted": bool(gain_deltas and not gain_verdict["accepted"]),
            "verification": gain_after, "verification_verdict": gain_verdict,
            "response_correction": correction_plan,
            "correction_deltas_db": correction_deltas,
            "correction_verification": correction_after,
            "correction_verdict": correction_verdict,
            "final_deltas_db": final_gain_deltas,
            "original_level_db": original_level,
            "final_level_db": final_gain_level,
            "stage_output_peaks": (final_gain_sweep or {}).get("stage_output_peaks"),
        })
        stored_fine_accepted = bool(acceptance["fine_accepted"] and auto_apply)
        stored_reject_reason = acceptance["reject_reason"]
        if not auto_apply and stored_winner is incumbent_winner and round(float(best_delay), 2) != round(float(current_alignment), 2):
            stored_reject_reason = apply_decision
        fine_scan["accepted_winner"] = stored_winner
        fine_scan["fine_accepted"] = stored_fine_accepted
        fine_scan["reject_reason"] = stored_reject_reason
        candidate_ledger = (
            _auto_sub_candidate_ledger(
                sweep_results, final_scoring, mode="2.1", phase="coarse",
                roles={
                    "coarse_winner": final_coarse_winner,
                    "final_accepted_winner": stored_winner,
                },
                decision_pool=final_decision_pool,
                requested_incumbent={"delay_ms": current_alignment},
            )
            + _auto_sub_candidate_ledger(
                fine_results, final_scoring, mode="2.1", phase="fine",
                roles={"fine_winner": final_fine_winner, "final_accepted_winner": stored_winner},
                decision_pool=final_decision_pool,
                requested_incumbent={"delay_ms": current_alignment},
            )
        )

        job["status"] = "completed"
        job["message"] = (
            f"Applied: {best_delay} ms (score {winner['score_pct']:.0f} %)"
            if auto_apply
            else f"Suggested: {best_delay} ms (not applied: {confidence})"
        )
        _log_auto_sub_timing_summary(job)

        # Build baseline and confirmation measurements for before/after graph display
        baseline_measurement = None
        confirmation_measurement = None
        all_sweep_results = list(sweep_results) + list(fine_results)
        baseline_sweep = _auto_sub_result_for_delay(all_sweep_results, current_alignment)
        _offset_db = _auto_sub_shared_bass_offset(
            baseline_sweep.get("points_left") if baseline_sweep else [],
            baseline_sweep.get("points_right") if baseline_sweep else [],
        )
        if baseline_sweep and (_auto_sub_has_points(baseline_sweep, "points_left") or _auto_sub_has_points(baseline_sweep, "points_right")):
            baseline_measurement = _auto_sub_measurement_from_sweep(
                baseline_sweep, "Before", f"AutoSub Baseline ({current_alignment:.1f} ms)",
                offset_db=_offset_db,
            )
        confirm_delay = best_delay if auto_apply else current_alignment
        confirmation_sweep = final_gain_sweep if gain_verdict["accepted"] else _auto_sub_result_for_delay(all_sweep_results, confirm_delay)
        if confirmation_sweep and (_auto_sub_has_points(confirmation_sweep, "points_left") or _auto_sub_has_points(confirmation_sweep, "points_right")):
            confirm_label = "After" if auto_apply else "Current"
            confirmation_measurement = _auto_sub_measurement_from_sweep(
                confirmation_sweep, confirm_label, f"AutoSub {confirm_label} ({confirm_delay:.1f} ms)",
                offset_db=_offset_db,
            )

        job["result"] = {
            "original_alignment_ms": current_alignment,
            "suggested_alignment_ms": best_delay,
            "applied_alignment_ms": applied_delay,
            "applied_sub_alignment_ms": applied_delay,
            "applied": auto_apply,
            "auto_applied": auto_apply,
            "apply_decision": apply_decision,
            "winner_margin_pct": round(winner_margin_pct, 1),
            "score_gain_pct": round(score_gain_pct, 1) if score_gain_pct is not None else None,
            "original_score_pct": round(original_score_pct, 1) if original_score_pct is not None else None,
            "crossover_hz": fc,
            "confidence": confidence,
            "winner": winner,
            "coarse_winner": final_coarse_winner,
            "coarse_runner_up": coarse_runner_up,
            "fine_winner": final_fine_winner,
            "incumbent_winner": incumbent_winner,
            "incumbent_score": acceptance["incumbent_score"],
            "accepted_winner": stored_winner,
            "fine_accepted": stored_fine_accepted,
            "reject_reason": stored_reject_reason,
            "runner_up": final_scoring.get("runner_up"),
            "ranking": final_scoring["results"],
            "candidate_ledger": candidate_ledger,
            "sweep_count": total + 2,
            "candidate_count": coarse_total + len(fine_delays),
            "coarse_candidate_count": coarse_total,
            "fine_candidate_count": len(fine_delays),
            "coarse_sweep_count": coarse_sweep_total,
            "fine_sweep_count": len(fine_delays) * 2,
            "valid_count": len(combined_valid),
            "coarse_valid_count": len(valid),
            "fine_valid_count": len(fine_valid),
            "fine_scan": fine_scan,
            "baseline_measurement": baseline_measurement,
            "confirmation_measurement": confirmation_measurement,
        }

        logger.info(
            "Auto-sub optimize completed: fc=%sHz suggested=%.2fms applied=%s applied_delay=%.2fms combined_score=%.0f%% "
            "score_L=%.1f%% score_R=%.1f%% confidence=%s decision=%s fine_scan=%s",
            fc,
            best_delay,
            auto_apply,
            applied_delay,
            winner.get("score_pct", 0),
            winner.get("score_L_pct", 0) or 0,
            winner.get("score_R_pct", 0) or 0,
            confidence,
            apply_decision,
            fine_scan.get("status"),
        )

    except Exception as exc:
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        logger.exception("Auto-sub optimize failed")
        job["status"] = "failed"
        job["message"] = f"Auto Sub Optimize failed: {exc}"
        job["error"] = {"detail": str(exc)}
        await _restore_original_config()

    finally:
        if measurement_sr_session is not None:
            await measurement_sr_session.unregister_auto_sub(job_id)
        _finalize_autosub_job(job, job_id)
        try:
            _auto_sub_lock.release()
        except RuntimeError:
            pass  # Lock was not held
        # Schedule job cleanup after 10 minutes
        cleanup_job_id = job_id
        async def _cleanup_autosub_job():
            await asyncio.sleep(600)
            _AUTO_SUB_JOBS.pop(cleanup_job_id, None)
        asyncio.create_task(_cleanup_autosub_job())


@app.post("/api/easyeffects/compare")
async def save_easyeffects_compare(request: Request):
    ee_manager = _require_easyeffects_manager()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    compare = ee_manager.save_compare_state({
        "presetA": body.get("presetA", body.get("preset_a", "")),
        "presetB": body.get("presetB", body.get("preset_b", "")),
        "activeSide": body.get("activeSide", body.get("active_side")),
    })

    status = ee_manager.get_status()
    await manager.broadcast({"type": "easyeffects", "data": status})
    return {"status": "ok", "compare": compare}

@app.post("/api/easyeffects/presets/combine")
async def combine_easyeffects_presets(request: Request):
    ee_manager = _require_easyeffects_manager()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON body, expected {'presetName': '...', 'presetNames': ['Preset 1', 'Preset 2']}",
        )

    preset_name = (body.get("presetName") or body.get("preset_name") or "").strip()
    preset_names = body.get("presetNames", body.get("preset_names")) or []
    load_after_create = bool(body.get("loadAfterCreate", body.get("load_after_create", False)))

    if not preset_name:
        raise HTTPException(status_code=400, detail="presetName is required")
    if not isinstance(preset_names, list):
        raise HTTPException(status_code=400, detail="presetNames must be an array")

    try:
        created = ee_manager.combine_presets(preset_name, preset_names)
        status = await _finish_easyeffects_preset_mutation(
            load_after_create=load_after_create,
            preset_name=created["name"],
            refresh_reason="combine-presets",
            refresh_only_when_loaded=True,
        )
        return {
            "status": "ok",
            "preset": created,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        _raise_easyeffects_http_error(e)

@app.post("/api/easyeffects/presets/load")
async def load_easyeffects_preset(request: Request):
    global easyeffects_preset_load_lock
    ee_manager = _require_easyeffects_manager()

    try:
        body = await request.json()
        preset_name = (body.get("preset_name") or "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body, expected {\"preset_name\": \"...\"}")

    if not preset_name:
        raise HTTPException(status_code=400, detail="preset_name is required")

    if easyeffects_preset_load_lock is None:
        easyeffects_preset_load_lock = asyncio.Lock()

    try:
        async with easyeffects_preset_load_lock:
            ee_manager.load_preset(preset_name)
            compare = ee_manager.load_compare_state()
            if compare.get("presetA") == preset_name:
                compare["activeSide"] = "A"
                ee_manager.save_compare_state(compare)
            elif compare.get("presetB") == preset_name:
                compare["activeSide"] = "B"
                ee_manager.save_compare_state(compare)
            status = ee_manager.get_status()
        if subwoofer_runtime is not None and subwoofer_runtime.snapshot().get("active"):
            await subwoofer_runtime._reclean_guarded(skip_if_locked=False)
        await manager.broadcast({"type": "easyeffects", "data": status})
        schedule_peak_monitor_refresh_after_effects_change("preset-load")
        return {"status": "ok", "active_preset": preset_name, "compare": status.get("compare")}
    except (FileNotFoundError, RuntimeError) as e:
        _raise_easyeffects_http_error(e)

@app.post("/api/easyeffects/irs/upload")
async def upload_easyeffects_ir(file: UploadFile = File(...)):
    ee_manager = _require_easyeffects_manager()

    tmp_path = None
    try:
        suffix = Path(file.filename or "upload.ir").suffix
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)

        uploaded = ee_manager.upload_ir(tmp_path, file.filename or tmp_path.name)
        status = ee_manager.get_status()
        await manager.broadcast({"type": "easyeffects", "data": status})
        schedule_peak_monitor_refresh_after_effects_change("ir-upload")
        return {"status": "ok", "ir": uploaded}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"EasyEffects IR upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

@app.post("/api/easyeffects/presets/create-convolver")
async def create_convolver_preset(
    preset_name: str = Form(...),
    ir_filename: str = Form(...),
    load_after_create: bool = Form(False),
    limiter_enabled: bool = Form(False),
    headroom_enabled: bool = Form(False),
    headroom_gain_db: float = Form(-3.0),
    autogain_enabled: bool = Form(False),
    autogain_target_db: float = Form(-12.0),
    delay_enabled: bool = Form(False),
    delay_left_ms: float = Form(0.0),
    delay_right_ms: float = Form(0.0),
    tone_effect_enabled: bool = Form(False),
    tone_effect_mode: str = Form("crystalizer"),
):
    ee_manager = _require_easyeffects_manager()

    extras = _effects_extras_from_form(
        limiter_enabled=limiter_enabled,
        headroom_enabled=headroom_enabled,
        headroom_gain_db=headroom_gain_db,
        autogain_enabled=autogain_enabled,
        autogain_target_db=autogain_target_db,
        delay_enabled=delay_enabled,
        delay_left_ms=delay_left_ms,
        delay_right_ms=delay_right_ms,
        tone_effect_enabled=tone_effect_enabled,
        tone_effect_mode=tone_effect_mode,
    )

    try:
        created = ee_manager.create_convolver_preset(preset_name, ir_filename, extras=extras)
        status = await _finish_easyeffects_preset_mutation(
            load_after_create=load_after_create,
            preset_name=created["name"],
            refresh_reason="create-convolver",
        )
        return {
            "status": "ok",
            "preset": created,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        _raise_easyeffects_http_error(e)

@app.post("/api/easyeffects/presets/import-json")
async def import_easyeffects_preset_json(
    file: UploadFile = File(...),
    load_after_create: bool = Form(False),
):
    ee_manager = _require_easyeffects_manager()

    try:
        content = (await file.read()).decode("utf-8-sig")
        created = ee_manager.import_preset_json(file.filename or "preset.json", content)
        status = await _finish_easyeffects_preset_mutation(
            load_after_create=load_after_create,
            preset_name=created["name"],
            refresh_reason="import-preset-json",
        )
        return {
            "status": "ok",
            "preset": created,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Preset JSON is not valid UTF-8 text: {e}")
    except (ValueError, RuntimeError) as e:
        _raise_easyeffects_http_error(e)

@app.post("/api/easyeffects/presets/import-bundle")
async def import_easyeffects_preset_bundle(
    file: UploadFile = File(...),
    load_after_create: bool = Form(False),
):
    ee_manager = _require_easyeffects_manager()

    with tempfile.NamedTemporaryFile(prefix="fxroute-preset-import-", suffix=".zip", delete=False) as temp_file:
        temp_zip_path = Path(temp_file.name)
        temp_file.write(await file.read())

    try:
        with zipfile.ZipFile(temp_zip_path) as archive:
            if archive.testzip() is not None:
                raise HTTPException(status_code=400, detail="Invalid ZIP archive")
            safe_members = []
            for member in archive.infolist():
                safe_relative = _is_safe_relative_zip_path(member.filename)
                if safe_relative is None or member.is_dir():
                    continue
                safe_members.append((member, safe_relative))

            json_members = [(member, rel) for member, rel in safe_members if rel.suffix.lower() == ".json" and rel.name.lower() != "manifest.json"]
            preferred_json = next(((member, rel) for member, rel in json_members if rel.name.lower() == "preset.json"), None)
            if preferred_json is None:
                preferred_json = json_members[0] if len(json_members) == 1 else None
            if preferred_json is None:
                raise HTTPException(status_code=400, detail="Preset bundle must contain exactly one preset JSON")

            preset_member, preset_rel = preferred_json
            preset_text = archive.read(preset_member).decode("utf-8-sig")
            try:
                preset_payload = json.loads(preset_text)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Preset JSON is invalid: {e}") from e
            kernel_names = ee_manager._extract_kernel_names_from_payload(preset_payload if isinstance(preset_payload, dict) else None)

            ee_manager.irs_dir.mkdir(parents=True, exist_ok=True)
            imported_irs = []
            ir_members_by_stem = {}
            for member, rel in safe_members:
                if rel.suffix.lower() not in {".irs", ".wav"}:
                    continue
                clean_ir_name = Path(rel.name).name
                stem = Path(clean_ir_name).stem
                if kernel_names and stem not in kernel_names:
                    continue
                existing = ir_members_by_stem.get(stem)
                if existing is None or rel.suffix.lower() == ".irs":
                    ir_members_by_stem[stem] = (member, clean_ir_name)

            for _, (member, clean_ir_name) in sorted(ir_members_by_stem.items()):
                destination = ee_manager.irs_dir / clean_ir_name
                with archive.open(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
                imported_irs.append(destination.name)

            missing_kernels = [name for name in sorted(kernel_names) if not ee_manager._find_ir_paths_for_kernel_name(name)]
            if missing_kernels:
                raise HTTPException(status_code=400, detail=f"Preset bundle is missing IR file(s): {', '.join(missing_kernels)}")

            preset_filename = preset_rel.name if preset_rel.name.lower() != "preset.json" else (Path(file.filename or "preset.json").stem + ".json")
            created = ee_manager.import_preset_json(preset_filename, preset_text)
            status = await _finish_easyeffects_preset_mutation(
                load_after_create=load_after_create,
                preset_name=created["name"],
                refresh_reason="import-preset-bundle",
            )
            return {
                "status": "ok",
                "preset": created,
                "irs": imported_irs,
                "loaded": bool(load_after_create),
                "active_preset": status.get("active_preset"),
            }
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid ZIP archive")
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Preset JSON is not valid UTF-8 text: {e}")
    except (ValueError, RuntimeError) as e:
        _raise_easyeffects_http_error(e)
    finally:
        temp_zip_path.unlink(missing_ok=True)

@app.post("/api/easyeffects/presets/create-with-ir")
async def create_convolver_preset_with_ir(
    preset_name: str = Form(...),
    load_after_create: bool = Form(False),
    limiter_enabled: bool = Form(False),
    headroom_enabled: bool = Form(False),
    headroom_gain_db: float = Form(-3.0),
    autogain_enabled: bool = Form(False),
    autogain_target_db: float = Form(-12.0),
    delay_enabled: bool = Form(False),
    delay_left_ms: float = Form(0.0),
    delay_right_ms: float = Form(0.0),
    bass_enabled: bool = Form(False),
    bass_amount: float = Form(0.0),
    tone_effect_enabled: bool = Form(False),
    tone_effect_mode: str = Form("crystalizer"),
    file: UploadFile = File(...),
):
    ee_manager = _require_easyeffects_manager()

    extras = _effects_extras_from_form(
        limiter_enabled=limiter_enabled,
        headroom_enabled=headroom_enabled,
        headroom_gain_db=headroom_gain_db,
        autogain_enabled=autogain_enabled,
        autogain_target_db=autogain_target_db,
        delay_enabled=delay_enabled,
        delay_left_ms=delay_left_ms,
        delay_right_ms=delay_right_ms,
        bass_enabled=bass_enabled,
        bass_amount=bass_amount,
        tone_effect_enabled=tone_effect_enabled,
        tone_effect_mode=tone_effect_mode,
    )

    tmp_path = None
    try:
        suffix = Path(file.filename or "upload.ir").suffix
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)

        created = ee_manager.create_convolver_preset_with_upload(
            preset_name,
            tmp_path,
            file.filename or tmp_path.name,
            extras=extras,
        )
        status = await _finish_easyeffects_preset_mutation(
            load_after_create=load_after_create,
            preset_name=created["preset"]["name"],
            refresh_reason="create-with-ir",
        )
        return {
            "status": "ok",
            "ir": created["ir"],
            "preset": created["preset"],
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        _raise_easyeffects_http_error(e)
    except Exception as e:
        logger.error(f"EasyEffects create-with-ir failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

@app.post("/api/easyeffects/presets/create-peq")
async def create_peq_preset(request: Request):
    ee_manager = _require_easyeffects_manager()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON body, expected {'presetName': '...', 'peq': {...}, 'loadAfterCreate': true|false}",
        )

    preset_name = (body.get("presetName") or body.get("preset_name") or "").strip()
    peq_definition = body.get("peq")
    load_after_create = bool(body.get("loadAfterCreate", body.get("load_after_create", False)))
    extras = _parse_effects_extras_from_json(body)

    if not preset_name:
        raise HTTPException(status_code=400, detail="presetName is required")
    if peq_definition is None:
        raise HTTPException(status_code=400, detail="peq is required")

    try:
        created = ee_manager.create_peq_preset(preset_name, peq_definition, extras=extras)
        status = await _finish_easyeffects_preset_mutation(
            load_after_create=load_after_create,
            preset_name=created["name"],
            refresh_reason="create-peq",
        )
        return {
            "status": "ok",
            "preset": created,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        _raise_easyeffects_http_error(e)

@app.post("/api/easyeffects/presets/import-rew-peq")
async def import_rew_peq_preset(
    preset_name: str = Form(...),
    load_after_create: bool = Form(False),
    limiter_enabled: bool = Form(False),
    headroom_enabled: bool = Form(False),
    headroom_gain_db: float = Form(-3.0),
    autogain_enabled: bool = Form(False),
    autogain_target_db: float = Form(-12.0),
    delay_enabled: bool = Form(False),
    delay_left_ms: float = Form(0.0),
    delay_right_ms: float = Form(0.0),
    bass_enabled: bool = Form(False),
    bass_amount: float = Form(0.0),
    tone_effect_enabled: bool = Form(False),
    tone_effect_mode: str = Form("crystalizer"),
    file: UploadFile = File(...),
):
    ee_manager = _require_easyeffects_manager()

    try:
        content = await file.read()
        rew_text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="REW import file must be UTF-8 text")

    if not preset_name.strip():
        raise HTTPException(status_code=400, detail="preset_name is required")

    extras = _effects_extras_from_form(
        limiter_enabled=limiter_enabled,
        headroom_enabled=headroom_enabled,
        headroom_gain_db=headroom_gain_db,
        autogain_enabled=autogain_enabled,
        autogain_target_db=autogain_target_db,
        delay_enabled=delay_enabled,
        delay_left_ms=delay_left_ms,
        delay_right_ms=delay_right_ms,
        bass_enabled=bass_enabled,
        bass_amount=bass_amount,
        tone_effect_enabled=tone_effect_enabled,
        tone_effect_mode=tone_effect_mode,
    )

    try:
        created = ee_manager.create_peq_preset_from_rew_text(preset_name, rew_text, extras=extras)
        status = await _finish_easyeffects_preset_mutation(
            load_after_create=load_after_create,
            preset_name=created["name"],
            refresh_reason="import-rew-peq",
        )
        return {
            "status": "ok",
            "preset": created,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
    except (ValueError, RuntimeError) as e:
        _raise_easyeffects_http_error(e)

@app.post("/api/easyeffects/presets/import-filter-dual")
async def import_dual_filter_preset(
    preset_name: str = Form(...),
    left_text: str = Form(""),
    right_text: str = Form(""),
    load_after_create: bool = Form(False),
    limiter_enabled: bool = Form(False),
    headroom_enabled: bool = Form(False),
    headroom_gain_db: float = Form(-3.0),
    autogain_enabled: bool = Form(False),
    autogain_target_db: float = Form(-12.0),
    delay_enabled: bool = Form(False),
    delay_left_ms: float = Form(0.0),
    delay_right_ms: float = Form(0.0),
    bass_enabled: bool = Form(False),
    bass_amount: float = Form(0.0),
    tone_effect_enabled: bool = Form(False),
    tone_effect_mode: str = Form("crystalizer"),
    left_file: Optional[UploadFile] = File(None),
    right_file: Optional[UploadFile] = File(None),
):
    ee_manager = _require_easyeffects_manager()

    if not preset_name.strip():
        raise HTTPException(status_code=400, detail="preset_name is required")

    extras = _effects_extras_from_form(
        limiter_enabled=limiter_enabled,
        headroom_enabled=headroom_enabled,
        headroom_gain_db=headroom_gain_db,
        autogain_enabled=autogain_enabled,
        autogain_target_db=autogain_target_db,
        delay_enabled=delay_enabled,
        delay_left_ms=delay_left_ms,
        delay_right_ms=delay_right_ms,
        bass_enabled=bass_enabled,
        bass_amount=bass_amount,
        tone_effect_enabled=tone_effect_enabled,
        tone_effect_mode=tone_effect_mode,
    )

    def _detect_upload_kind(upload: Optional[UploadFile]) -> Optional[str]:
        if not upload or not (upload.filename or "").strip():
            return None
        suffix = Path(upload.filename).suffix.lower()
        if suffix in {".txt"}:
            return "rew-text"
        if suffix in {".irs", ".wav"}:
            return "convolver"
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {upload.filename}")

    left_kind = _detect_upload_kind(left_file)
    right_kind = _detect_upload_kind(right_file)

    if bool(left_kind) != bool(right_kind):
        raise HTTPException(status_code=400, detail="Provide both Left and Right files, or neither")

    tmp_paths = []
    try:
        if left_kind == "convolver" and right_kind == "convolver":
            import tempfile

            async def _save_temp(upload: UploadFile) -> Path:
                suffix = Path(upload.filename or "upload.ir").suffix or ".ir"
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(await upload.read())
                    return Path(tmp.name)

            left_tmp = await _save_temp(left_file)
            right_tmp = await _save_temp(right_file)
            tmp_paths.extend([left_tmp, right_tmp])

            created = ee_manager.create_convolver_preset_with_dual_uploads(
                preset_name,
                left_tmp,
                left_file.filename or left_tmp.name,
                right_tmp,
                right_file.filename or right_tmp.name,
                extras=extras,
            )
            import_kind = "dual-convolver"
        else:
            if left_kind == "rew-text" and right_kind == "rew-text":
                try:
                    left_text = (await left_file.read()).decode("utf-8-sig")
                    right_text = (await right_file.read()).decode("utf-8-sig")
                except UnicodeDecodeError:
                    raise HTTPException(status_code=400, detail="Dual REW import files must be UTF-8 text")

            left_text = str(left_text or "").strip()
            right_text = str(right_text or "").strip()
            if not left_text or not right_text:
                raise HTTPException(status_code=400, detail="Provide Left and Right REW text, or Left and Right .irs/.wav files")

            created = ee_manager.create_dual_peq_preset_from_rew_texts(
                preset_name,
                left_text,
                right_text,
                extras=extras,
            )
            import_kind = "dual-peq"

        created_preset = created["preset"] if import_kind == "dual-convolver" else created
        status = await _finish_easyeffects_preset_mutation(
            load_after_create=load_after_create,
            preset_name=created_preset["name"],
            refresh_reason="import-filter-dual",
        )
        return {
            "status": "ok",
            "import_kind": import_kind,
            "preset": created_preset,
            "ir": created.get("ir") if isinstance(created, dict) else None,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
    except (ValueError, RuntimeError) as e:
        _raise_easyeffects_http_error(e)
    finally:
        for tmp_path in tmp_paths:
            tmp_path.unlink(missing_ok=True)

@app.post("/api/easyeffects/presets/delete")
async def delete_easyeffects_preset(request: Request):
    ee_manager = _require_easyeffects_manager()

    try:
        body = await request.json()
        preset_name = (body.get("preset_name") or "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body, expected {\"preset_name\": \"...\"}")

    if not preset_name:
        raise HTTPException(status_code=400, detail="preset_name is required")

    try:
        ee_manager.delete_preset(preset_name)
        status = ee_manager.get_status()
        await manager.broadcast({"type": "easyeffects", "data": status})
        schedule_peak_monitor_refresh_after_effects_change("preset-delete")
        return {"status": "ok", "deleted": preset_name}
    except (FileNotFoundError, ValueError) as e:
        _raise_easyeffects_http_error(e)

@app.get("/api/library/status")
async def library_status():
    global library_scanner
    if library_scanner:
        return library_scanner.status()
    return {"scanning": False, "track_count": 0, "error": "Library scanner not initialized"}


@app.post("/api/library/refresh")
async def refresh_library():
    global library_scanner
    if library_scanner:
        if not library_scanner.scanning:
            library_scanner.prepare_scan_status()
            asyncio.create_task(asyncio.to_thread(library_scanner.refresh, True))
        return {"status": "scanning", **library_scanner.status()}
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
    global latest_spotify_state
    data = await get_spotify_ui_state()
    latest_spotify_state = data
    await sync_peak_monitor_for_spotify_state(data)
    return data


@app.post("/api/spotify/play")
async def api_spotify_play():
    request = TransitionRequest(
        operation="spotify-play",
        source="spotify",
        target_rate=SPOTIFY_PREARM_SAMPLE_RATE_HZ,
        should_play=True,
        rate_change=_coordinator_rate_change(SPOTIFY_PREARM_SAMPLE_RATE_HZ),
        reload_source=True,
        detail="api-spotify-play",
    )
    try:
        await _run_coordinated_transition(request)
    except PlaybackTransitionFailure as exc:
        raise _transition_error_http(exc) from exc
    global current_footer_owner, latest_spotify_state
    current_footer_owner = "spotify"
    latest_spotify_state = await get_spotify_ui_state()
    return await broadcast_spotify_state(latest_spotify_state)


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

    request = TransitionRequest(
        operation="spotify-toggle",
        source="spotify",
        target_rate=SPOTIFY_PREARM_SAMPLE_RATE_HZ,
        should_play=True,
        rate_change=_coordinator_rate_change(SPOTIFY_PREARM_SAMPLE_RATE_HZ),
        reload_source=True,
        detail="api-spotify-toggle",
    )
    try:
        await _run_coordinated_transition(request)
    except PlaybackTransitionFailure as exc:
        raise _transition_error_http(exc) from exc
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
        volume_result = _set_canonical_output_volume(volume)
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
    await websocket.send_text(json.dumps({"type": "init", "data": {"player": {"state": build_playback_payload()}, "spotify": await get_spotify_ui_state()}}))
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            if text is not None:
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"WebSocket error: {e}")
    finally:
        manager.disconnect(websocket)

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

if __name__ == "__main__":
    settings = get_settings()
    run_server()
