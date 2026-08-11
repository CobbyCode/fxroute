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
import random
import subprocess
import tempfile
import zipfile
import samplerate_orchestration
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Mapping, Optional
from urllib.parse import quote, unquote, urlparse
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from config import get_settings
from radio_metadata import RadioMetadataService
from uploads import (
    EASYEEFFECTS_BUNDLE_MAX_BYTES,
    EASYEEFFECTS_IR_MAX_BYTES,
    EASYEEFFECTS_PRESET_TEXT_MAX_BYTES,
    UploadTooLargeError,
    read_upload,
    save_upload_to_file,
)

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
    if measurement_sr_session is None or not measurement_sr_session.active:
        return False
    if not isinstance(expected_rate, int) or expected_rate == measurement_sr_session.measurement_rate:
        return False
    # An open-but-idle measurement window (no running sweep/auto-sub/SPL
    # job) must not block playback rate changes; the next measurement
    # entry/preflight re-establishes its rate.
    return measurement_sr_session.has_active_jobs


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


_RATE_RENEGOTIATION_TRIGGER_WAIT_MS = 2500


def _rate_renegotiation_trigger_path(sample_rate: int) -> Path:
    return Path(tempfile.gettempdir()) / f"fxroute-rate-renegotiation-trigger-{sample_rate}.wav"


def _ensure_rate_renegotiation_trigger_file(sample_rate: int) -> Path | None:
    """Generate (once) a short silent stream used to wake an idle hardware sink.

    A fully idle/suspended hardware sink ignores ``clock.force-rate`` writes
    and suspend/resume pulses; the only proven renegotiation trigger is a
    brief silent stream, after which the sink keeps the forced rate.
    """
    path = _rate_renegotiation_trigger_path(sample_rate)
    if path.exists():
        return path
    try:
        generated = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=stereo",
                "-t", "0.8", "-c:a", "pcm_s16le", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        logger.warning("Rate renegotiation trigger generation failed: %s", exc)
        return None
    if generated.returncode != 0:
        logger.warning(
            "Rate renegotiation trigger generation failed: %s",
            (generated.stderr or "").strip(),
        )
        return None
    return path


async def _trigger_idle_sink_renegotiation(sample_rate: int) -> bool:
    """Renegotiate an idle/suspended sink to the forced rate with a silent stream."""
    path = _ensure_rate_renegotiation_trigger_file(sample_rate)
    if path is None:
        return False
    try:
        subprocess.Popen(
            ["pw-play", "--volume=0", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.warning("Rate renegotiation trigger playback failed: %s", exc)
        return False
    logger.info(
        "Rate renegotiation trigger played: rate=%s (silent stream, volume 0)",
        sample_rate,
    )
    return await _wait_for_samplerate_alignment(
        sample_rate, timeout_ms=_RATE_RENEGOTIATION_TRIGGER_WAIT_MS
    )


async def _reconcile_transition_sink_rate(target_rate: int, *, reason: str) -> bool:
    """Re-establish the hardware sink rate before a transition stage commits.

    The effects/helper graph rebuild inside a transition can leave the sink
    suspended at the configured default rate while ``clock.force-rate`` already
    points at the target.  A suspended sink ignores force-rate writes and
    suspend/resume pulses; the bounded fallback below plays a short silent
    stream, the only proven renegotiation trigger on an idle graph.
    """
    try:
        status = dict(get_samplerate_status())
    except Exception:
        status = {}
    if samplerate.playback_rate_aligned(status, target_rate):
        return True
    aligned = await _ensure_playback_samplerate_force(
        target_rate,
        reason=f"coordinator-{reason}",
        policy=samplerate_orchestration.RADIO_POLICY,
    )
    if not aligned:
        aligned = await _trigger_idle_sink_renegotiation(target_rate)
    if not aligned:
        return False
    try:
        status = dict(get_samplerate_status())
    except Exception:
        status = {}
    return bool(samplerate.playback_rate_aligned(status, target_rate))


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
    PlayRequest,
)
from player import get_player, MPVNotInstalledError, normalize_stream_info
from radio_api import _station_api_payload, router as radio_api_router
from stations import get_stations
import playlist_io
import sink_inputs
import playback_state
import samplerate
from library import (
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
    MeasurementStore,
)
from peak_monitor import EasyEffectsPeakMonitor
from subwoofer_runtime import (
    Subwoofer21Runtime,
    SubwooferRuntimeConfig,
    _contains_link,
)
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
    normalize_sample_rate_policy,
    persist_audio_output_mode,
    persist_sample_rate_policy,
    prepare_audio_output_mode,
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
    next_track as spotify_next,
    previous as spotify_previous,
    shuffle_toggle as spotify_shuffle_toggle,
    loop_cycle as spotify_loop_cycle,
    seek_to as spotify_seek_to,
)
from system_volume import SystemVolumeError, get_output_volume, set_output_volume, get_status_volume, start_volume_read_monitor

logger = logging.getLogger(__name__)

import zip_album
import install_info
import effects_extras
import spl_calibration
import autosub
import measurement_session
from measurement_session import (
    MeasurementServices,
    MeasurementSampleRateSession,
    _measurement_helper_snapshot_summary,
    _wait_for_selected_output_effective_rate,
)
from library_api import (
    LibraryApiRuntime,
    _record_local_track_started,
    _track_cover_available,
    _cleanup_temp_file,
    configure_runtime as configure_library_api_runtime,
    router as library_api_router,
)


# Global instances (initialized on startup)
settings = None
player_instance = None
library_scanner = None
downloader = None
easyeffects_manager = None
measurement_store = None
measurement_sr_session = None
measurement_watchdog_task = None
library_scan_task = None
peak_monitor = None
subwoofer_runtime = None
subwoofer_runtime_link_watch_task = None
hardware_controller = None
peak_monitor_playback_armed = False
peak_monitor_transition_lock = None
peak_monitor_context_signature = None
easyeffects_preset_load_lock = None
# Serializes threaded EasyEffects mutations (convolver IR upload/create)
# so concurrent HTTP requests cannot interleave filesystem/preset state
# changes that used to run serially in the event loop.
easyeffects_mutation_lock = None
source_transition_lock = None


def _easyeffects_mutation_lock() -> asyncio.Lock:
    global easyeffects_mutation_lock
    if easyeffects_mutation_lock is None:
        easyeffects_mutation_lock = asyncio.Lock()
    return easyeffects_mutation_lock


# Serializes canonical volume writes (/api/volume, /api/spotify/volume) so
# concurrent requests cannot interleave their set -> verified get -> status
# cache publish sequences.
canonical_volume_write_lock = None


def _canonical_volume_write_lock() -> asyncio.Lock:
    global canonical_volume_write_lock
    if canonical_volume_write_lock is None:
        canonical_volume_write_lock = asyncio.Lock()
    return canonical_volume_write_lock


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
lifecycle_background_tasks: set[asyncio.Task] = set()
library_refresh_tasks: set[asyncio.Task] = set()
latest_player_state_seq_seen = 0
playback_transition_epoch = 0
playback_transition_pending_attempts = 0
# Warte-Primitiv fuer Ended-Callbacks: gesetzt, sobald keine
# Playback-Transition-Instanz mehr in flight ist.  asyncio.Event bindet sich
# an den ersten benutzten Event-Loop; Tests nutzen pro Test einen frischen
# Loop, daher wird das Signal pro Loop gehalten (Produktion: genau ein Loop).
# Kein zweites Transition-Lock.
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
playback_intent_generation = 0
# Publizierter Playback-Kontext-Token: wechselt NUR, wenn ein neuer
# autoritativer Playback-Kontext (Local/Radio/Spotify/Queue-Track) an der
# App-Commit-Boundary vollstaendig publiziert wurde.  Coordinator-
# Operationen ohne Source-/Track-Wechsel (output-mode, measurement-entry/
# restore, sample-rate-policy, recovery, graph-reconcile) lassen ihn
# unveraendert; fehlgeschlagene Attempts ebenso.
playback_context_commit_id: str | None = None
current_track_info = None
last_track_info = None
last_radio_track_info = None
radio_reconnect_task = None
radio_reconnect_attempts = 0
radio_reconnect_url = None
radio_reconnect_active_since = 0.0
radio_metadata_service = RadioMetadataService()
samplerate_drift_signature: tuple[Any, ...] | None = None
samplerate_drift_readbacks = 0
playback_queue = []
playback_queue_original = []
playback_queue_index = -1
playback_queue_mode = "app_replace"
queue_advancing = False
playback_queue_loop = False
playback_queue_shuffle = False
single_track_loop = False

configure_library_api_runtime(LibraryApiRuntime(
    get_scanner=lambda: library_scanner,
    get_settings=lambda: settings,
    run_blocking=_drain_worker,
))
spl_calibration.configure_runtime(spl_calibration.SplCalibrationDependencies(
    get_measurement_store=lambda: measurement_store,
    get_measurement_session=lambda: measurement_sr_session,
    get_easyeffects_manager=lambda: easyeffects_manager,
    require_easyeffects_manager=lambda: _require_easyeffects_manager(),
    get_output_volume=lambda: get_output_volume(),
    set_output_volume=lambda value: set_output_volume(value),
    read_measurement_settings=lambda: measurement_session._read_measurement_setup_settings(),
    measurement_entry_preflight=lambda rate: measurement_session._measurement_entry_preflight(rate),
    run_easyeffects_mutation=lambda func: _run_locked_worker(
        _easyeffects_mutation_lock(), func
    ),
))
measurement_session.configure_services(MeasurementServices(
    get_store=lambda: measurement_store,
    get_session=lambda: measurement_sr_session,
    auto_sub_active=lambda: autosub.is_optimization_active(),
))


def _begin_playback_transition_attempt() -> int:
    global playback_transition_epoch, playback_transition_pending_attempts
    playback_transition_epoch += 1
    playback_transition_pending_attempts += 1
    _playback_settled_event().clear()
    return playback_transition_epoch


def _end_playback_transition_attempt() -> None:
    global playback_transition_pending_attempts
    playback_transition_pending_attempts -= 1
    if playback_transition_pending_attempts < 0:
        playback_transition_pending_attempts = 0
        logger.critical("playback transition attempt accounting underflow")
    if playback_transition_pending_attempts == 0:
        _playback_settled_event().set()


def _capture_playback_transition_epoch() -> int | None:
    """Capture a playback-context token; None while an attempt is in flight.

    A token captured while any attempt is active must stay invalid forever,
    matching the legacy odd/even generation contract: only idle captures may
    ever become a committed context.
    """
    if playback_transition_pending_attempts > 0:
        return None
    return playback_transition_epoch


def _current_playback_commit_id() -> str | None:
    """Return the published playback-context token.

    Unlike the Coordinator's ``last_successful_commit_id`` (which advances on
    every committed Coordinator operation, including output-mode-switch,
    measurement-entry/restore, sample-rate-policy and recovery), this token
    is only published at the application commit boundary together with the
    authoritative playback globals.  It stays unchanged while an attempt is
    running, after it failed, and after non-source-changing commits.
    """
    return playback_context_commit_id


def _publish_playback_context_commit(commit_token: str | None) -> None:
    """Publish the playback-context token exactly once at the app boundary.

    Synchronous: callers invoke this immediately after all globals of the new
    authoritative playback context were committed and before any further
    await, so the Ended-Waiter can never observe a window with a new token
    but old playback globals (or vice versa).
    """
    global playback_context_commit_id
    if commit_token:
        playback_context_commit_id = str(commit_token)


async def _wait_playback_transition_settled() -> None:
    """Wait until no playback-transition attempt is pending (terminal state).

    Waits on the settled event which is cleared on every attempt start and
    set when the last attempt finished.  Multiple queued attempts (nested or
    serialized behind the Coordinator lock) are all covered, because the
    counter only reaches zero after the final attempt drained.  Never holds
    the Coordinator lock and never blocks the event loop.
    """
    while playback_transition_pending_attempts > 0:
        event = _playback_settled_event()
        if not event.is_set():
            await event.wait()
        else:
            # Defensive yield: a set event with pending attempts violates the
            # begin/end invariant; re-check after yielding instead of spinning.
            await asyncio.sleep(0)


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
            samplerate.playback_rate_aligned(rate, target_rate)
        )
        result["repairable_link_loss"] = _measurement_session_link_loss_is_repairable(
            result,
            target_rate=target_rate,
        )
        return result

    async def reconcile_measurement_session_graph(self, _target_rate: int) -> None:
        """Repair only existing production links; never reload EE or the helper."""
        diagnosis = await _playback_graph_diagnosis(
            target_rate=_target_rate,
            require_source=False,
        )
        if diagnosis.get("mode") in OUTPUT_MODE_SUBWOOFER_MODES:
            await _coordinator_reconcile_subwoofer_links_only()
        elif diagnosis.get("mode") == OUTPUT_MODE_STEREO:
            await _repair_stereo_output_links_once(diagnosis)
        else:
            raise RuntimeError(
                "measurement session graph reconciliation has no mode repair path"
            )

    async def read_transition_snapshot(self, request: TransitionRequest) -> dict[str, Any]:
        self._staged_target_url = None
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
        global current_track_info, current_footer_owner, playback_queue_mode

        if request.source not in {"local", "radio"}:
            if request.source != "spotify":
                return
            # A failed Spotify handoff already quieted and stopped the
            # previously committed Local/Radio source and cleared its track
            # context before the Spotify start was verified (quiet_old_source
            # -> pause_local_playback_for_spotify_broadcast).  Restore the
            # pre-transition committed source physically (sample rate, MPV
            # load, pause/play state, volume) from the Coordinator snapshot
            # so a failed Spotify start never loses both sources.  The
            # committed queue, last_track_info and radio-reconnect state were
            # never touched by the handoff and stay as they are.  On success
            # this returns True so the Coordinator restores the output gate
            # instead of latching a failure; on restore failure the existing
            # failure latch keeps the safe state.
            previous_state = dict((snapshot or {}).get("player") or {})
            snapshot_track = dict((snapshot or {}).get("current_track") or {})
            if snapshot_track.get("source") in {"local", "radio"} and bool(
                previous_state.get("current_file")
                or previous_state.get("playing")
                or previous_state.get("paused")
                or previous_state.get("ended")
            ):
                restored = await self._restore_committed_source_after_failed_spotify(
                    request,
                    snapshot,
                    previous_state,
                    snapshot_track,
                    ensure_gate_closed=ensure_gate_closed,
                )
                if restored:
                    current_track_info = dict(snapshot_track)
                    current_footer_owner = "local"
                    _mark_player_state_authoritative(player_instance.state if player_instance else {})
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
                # The committed queue and track context stay valid: MPV still
                # exposes the exact pre-transition file and a staged queue
                # candidate was never published.  Nothing to invalidate.
                return

        # The target was staged, the old file disappeared, or the active
        # metadata no longer matches MPV. Stop the physical target first and
        # then invalidate only the active context. last_track_info is
        # deliberately untouched so the caller can offer a retry.  The
        # committed queue state is preserved.
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
            if playback_queue_mode == "native_mpv":
                try:
                    # MPV may hold a partially staged replacement playlist
                    # (or the committed playlist was already replaced by the
                    # staging).  Trim the transport to the current file.
                    _reduce_native_mpv_playlist_to_current()
                    _reset_mpv_loop_state()
                except Exception:
                    logger.warning(
                        "Failed to trim native playlist during transition abort",
                        exc_info=True,
                    )
                # After a staged failure the retained committed queue can no
                # longer be trusted as a complete MPV-native playlist, even
                # when the transport cleanup itself failed.  Normalize it to
                # app-owned navigation so the queue data stays usable.
                playback_queue_mode = "app_replace"

        current_track_info = None
        current_footer_owner = "local"
        _mark_player_state_authoritative(player_instance.state if player_instance else {})

    async def _restore_committed_source_after_failed_spotify(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any],
        previous_state: Mapping[str, Any],
        track: Mapping[str, Any],
        *,
        ensure_gate_closed: Callable[..., Awaitable[None]] | None = None,
    ) -> bool:
        """Physically restore the previously committed Local/Radio source and
        its full playback graph after a failed Spotify handoff.

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
        global playback_queue_mode
        native_committed = bool(
            playback_queue_mode == "native_mpv" and len(playback_queue) > 1
        )
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
            derived = _coordinator_target_rate(source, track)
            restore_target_rate = int(derived) if isinstance(derived, int) and derived > 0 else 0
        if restore_target_rate <= 0:
            logger.warning(
                "Spotify handoff source restore aborted: no authoritative committed "
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
            live_status = dict(get_samplerate_status())
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
                tuple(dict(item) for item in playback_queue) if native_committed else None
            ),
            native_queue_index=playback_queue_index if native_committed else None,
            native_queue_jump=None,
            native_queue_loop=(
                bool(playback_queue_loop or single_track_loop) if native_committed else False
            ),
            detail="spotify-abort-restore",
        )
        restored = False
        try:
            # A verify failure after a successful Spotify start can leave the
            # Spotify sink input active while the old rate and graph are
            # restored.  Quiesce it through the existing bounded release
            # helper before any old-graph stage touches the graph; if the
            # active Spotify source does not release within the existing
            # bound, the restore fails and the failure latch stays.
            if not await _wait_for_pipewire_spotify_release():
                logger.warning(
                    "Spotify handoff source restore aborted: active Spotify sink "
                    "input did not quiesce before the old source restore"
                )
                return False
            # The Coordinator-owned hardware gate must be physically closed
            # before ANY mutating restore stage (rate, effects/helper, MPV,
            # graph, volume) runs: the original Spotify transition may itself
            # have failed at output-gate-close, leaving the gate unverified.
            if ensure_gate_closed is not None:
                await ensure_gate_closed(stage="spotify-abort-restore-before-rate")
            await self.establish_target_rate(restore_request)
            if ensure_gate_closed is not None:
                await ensure_gate_closed(stage="spotify-abort-restore-after-rate")
            effects_state: dict[str, Any] = {}
            effects_result = await self.establish_effects_and_helper(restore_request)
            if isinstance(effects_result, Mapping):
                effects_state = dict(effects_result)
            dsp_reinitialized = bool(effects_state.get("dsp_reinitialized"))
            if ensure_gate_closed is not None:
                await ensure_gate_closed(stage="spotify-abort-restore-after-effects-helper")
            await self.prepare_target_source(restore_request)
            if ensure_gate_closed is not None:
                await ensure_gate_closed(stage="spotify-abort-restore-before-start")
            await self.start_target_source(restore_request)
            reconciler = getattr(self, "reconcile_post_start_graph", None)
            if callable(reconciler):
                post_state = await reconciler(restore_request)
                if not isinstance(post_state, Mapping) or not post_state.get(
                    "graph_complete", False
                ):
                    logger.warning(
                        "Spotify handoff source restore aborted: post-start graph "
                        "reconciliation did not confirm a complete graph"
                    )
                    return False
            graph_state = await self.verify_transition_graph(restore_request)
            if not bool(graph_state.get("committed", True)):
                logger.warning(
                    "Spotify handoff source restore aborted: staged graph readback "
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
                await ensure_gate_closed(stage="spotify-abort-restore-before-volume")
            await self.set_source_volume(100, "spotify-abort-restore")
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
                        "Spotify handoff source restore aborted: DSP stabilization "
                        "was not confirmed"
                    )
                    return False
            if ensure_gate_closed is not None:
                await ensure_gate_closed(stage="spotify-abort-restore-after-dsp")
            final_state = await self.verify_committed_transition(restore_request)
            if not bool(final_state.get("committed", True)):
                logger.warning(
                    "Spotify handoff source restore aborted: final commit readback "
                    "did not satisfy the commit contract"
                )
                return False
            try:
                source_volume = int(final_state.get("source_volume"))
            except (TypeError, ValueError):
                source_volume = None
            if source_volume != 100:
                logger.warning(
                    "Spotify handoff source restore aborted: final commit readback "
                    "did not positively confirm source volume 100: volume=%s",
                    final_state.get("source_volume"),
                )
                return False
            restored = True
        except Exception as exc:
            logger.warning("Spotify handoff source restore failed: %s", exc)
        if not restored and native_committed:
            # The native playlist could not be reconstructed; normalize to
            # the existing app-owned navigation so no later jump targets a
            # phantom MPV playlist (same contract as the staged abort path).
            try:
                _reduce_native_mpv_playlist_to_current()
                _reset_mpv_loop_state()
            except Exception:
                logger.warning(
                    "Failed to normalize native queue after failed Spotify restore",
                    exc_info=True,
                )
            playback_queue_mode = "app_replace"
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
        if expected_source == "spotify":
            expected_identities = _spotify_snapshot_identity_values(intent)
            if not expected_identities:
                expected_identities = _spotify_snapshot_identity_values({
                    "target_url": request.target_url,
                    "track_info": request.target_track,
                })
            return await _spotify_intent_matches_live_state(
                expected_identities,
                intent.get("intent_generation"),
            )

        expected_id = intent.get("id")
        if expected_id in {None, ""}:
            expected_id = None
        return _local_intent_matches_live_state(
            expected_source=expected_source,
            expected_id=expected_id,
            expected_url=intent.get("url") or intent.get("path") or request.target_url,
            expected_file=intent.get("current_file"),
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
                await spotify_pause()
                released = await _wait_for_pipewire_spotify_release()
                if not released:
                    await asyncio.sleep(SOURCE_HANDOFF_SETTLE_MS / 1000)
                return
            if request.operation in {"measurement-entry", "output-mode-switch", "sample-rate-policy"}:
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
            and request.operation not in {"measurement-entry", "output-mode-switch", "sample-rate-policy"}
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
            return samplerate.effective_playback_rate(live_rate, request.sample_rate_policy or None)

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
        attempt_epoch = request.attempt_epoch
        if not isinstance(attempt_epoch, int):
            attempt_epoch = playback_transition_epoch
        live_rate = await _wait_for_radio_live_rate_after_load(
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
            status = dict(get_samplerate_status())
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
        aligned = await _ensure_playback_samplerate_force(
            request.target_rate,
            f"coordinator:{request.operation}:{request.source}",
            allow_measurement_session=(request.operation == "measurement-restore"),
            policy=samplerate_orchestration.RADIO_POLICY,
        )
        if not aligned:
            aligned = await _trigger_idle_sink_renegotiation(request.target_rate)
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
        return await _coordinator_establish_effects_and_helper(request)

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
            request.operation in {"measurement-restore", "replay"}
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
                "Local playback position restored under output gate: "
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
        """Re-apply the canonical DSP work point after a rate/EE mutation.

        An output-mode switch may reload the compare preset or rebuild the
        EasyEffects graph (up to a full service restart), each of which can
        re-apply a stale preset loudness work point over the user volume.
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

        manager = easyeffects_manager
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
        async with _easyeffects_mutation_lock():
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
            await _drain_worker(
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
        source_required = bool(
            request.should_play
            or (request.operation == "output-mode-switch" and bool(request.target_url))
        )
        graph = await _playback_graph_diagnosis(
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
                    await _repair_stereo_output_links_once(graph)
                elif graph_mode in OUTPUT_MODE_SUBWOOFER_MODES:
                    await _coordinator_reconcile_subwoofer_links_only()
                else:
                    graph_mode = None
                if graph_mode is not None:
                    graph = await _playback_graph_diagnosis(
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
            source_rate = _coordinator_source_rate("spotify", request.target_track)
            spotify_stream_rate = await _wait_for_spotify_sink_input_samplerate(expected_rate=source_rate)
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
            try:
                effects_runtime = await self._read_and_validate_effects_runtime(extras)
            except RuntimeError:
                # Recoverable DSP work-point drift (e.g. a stale SPL-noise
                # state surviving an EasyEffects restart/preset reload):
                # re-apply the canonical runtime once under the still-closed
                # gate and re-validate before failing the transition.
                apply_runtime = getattr(
                    easyeffects_manager, "apply_autogain_loudness_runtime", None
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
                async with _easyeffects_mutation_lock():
                    extras = easyeffects_manager.load_global_extras()
                    await _drain_worker(
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
        status = dict(get_samplerate_status())
        if not samplerate.playback_rate_aligned(status, request.target_rate):
            await _reconcile_transition_sink_rate(
                request.target_rate, reason="measurement-entry"
            )
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
        if not samplerate.playback_rate_aligned(rate, target_rate):
            await _reconcile_transition_sink_rate(
                target_rate, reason="output-mode-switch"
            )
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
            source_rate = _coordinator_source_rate("spotify", request.target_track)
            spotify_stream_rate = await _wait_for_spotify_sink_input_samplerate(expected_rate=source_rate)
            if spotify_stream_rate != source_rate:
                raise RuntimeError(
                    "Spotify stream rate mismatch during output-mode commit: "
                    f"expected={source_rate} actual={spotify_stream_rate}"
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
        persist_audio_output_mode(old_config)
        if easyeffects_manager is not None and old_preset:
            current_preset = easyeffects_manager.get_active_preset()
            if current_preset != old_preset:
                await _load_easyeffects_preset(old_preset, convolver_sample_rate_hz=request.target_rate)
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


def _coordinator_source_rate(source: str, track: Mapping[str, Any] | None = None) -> int | None:
    track = track or {}
    if source == "spotify":
        return SPOTIFY_PREARM_SAMPLE_RATE_HZ
    if source == "radio":
        return int(track.get("sample_rate_hz") or RADIO_EXPECTED_SAMPLE_RATE_HZ)
    value = track.get("sample_rate_hz")
    return int(value) if isinstance(value, int) and value > 0 else None


def _coordinator_target_rate(source: str, track: Mapping[str, Any] | None = None) -> int | None:
    return samplerate.effective_playback_rate(_coordinator_source_rate(source, track))


def _sample_rate_policy_is_auto() -> bool:
    return samplerate.load_sample_rate_policy().get("mode") == "auto"


async def _transition_sample_rate_policy(policy: Mapping[str, Any], *, detail: str) -> None:
    overview = get_audio_output_overview()
    selected_output = overview.get("selected_output") or overview.get("current_output") or {}
    if (
        policy.get("mode") == "fixed"
        and policy.get("rate") not in (selected_output.get("supported_rates") or [])
    ):
        raise ValueError("Selected output does not support this sample rate")

    context = await _coordinator_current_playback_context()
    source = str(context.get("source") or "local")
    source_rate = _coordinator_source_rate(source, context.get("target_track"))
    if source in {"local", "radio"} and context.get("target_url"):
        live_source_rate = _get_player_audio_samplerate()
        if isinstance(live_source_rate, int) and live_source_rate > 0:
            source_rate = live_source_rate
    target_rate = samplerate.effective_playback_rate(source_rate, policy)
    if not isinstance(target_rate, int) or target_rate <= 0:
        status = get_samplerate_status()
        target_rate = status.get("active_rate") or status.get("force_rate")
    if not isinstance(target_rate, int) or target_rate <= 0:
        raise RuntimeError("current hardware sample rate is unavailable")

    await _run_coordinated_transition(TransitionRequest(
        operation="sample-rate-policy",
        source=source,
        target_rate=target_rate,
        target_url=context.get("target_url"),
        target_track=dict(context.get("target_track") or {}),
        should_play=bool(context.get("should_play")),
        rate_change=_coordinator_rate_change(target_rate),
        reload_source=False,
        detail=detail,
        output_mode_target=dict(overview),
        sample_rate_policy=dict(policy),
    ))


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
    return not samplerate.playback_rate_aligned(status, target_rate)


def _playback_transition_is_active() -> bool:
    return bool(
        playback_transition_coordinator is not None
        and playback_transition_coordinator.transition_active
    )


def _coordinator_commit_context_id() -> str | None:
    """Return the newest successful Coordinator commit context."""
    global coordinator_last_successful_commit_id
    context_id = getattr(playback_transition_coordinator, "last_successful_commit_id", None)
    if context_id:
        coordinator_last_successful_commit_id = str(context_id)
    return coordinator_last_successful_commit_id


async def _recovery_context_is_valid(request: TransitionRequest) -> bool:
    """Validate a watcher recovery against the still-committed live source."""
    expected_context = request.recovery_commit_context_id
    expected_source = request.recovery_source or request.source
    expected_url = request.recovery_url or request.target_url
    if not expected_context or expected_source != request.source or not expected_url:
        return False
    coordinator = playback_transition_coordinator
    context_validator = getattr(coordinator, "recovery_context_is_current", None)
    if callable(context_validator):
        if not context_validator(expected_context):
            # A failed transition latches the coordinator gate; while the
            # latch is held recovery_context_is_current() is always False and
            # watcher recoveries would be deadlocked forever.  A subwoofer
            # link repair against the still-committed context may re-enter:
            # the Coordinator's own gate-close/restore stages own the latch
            # (clear it on restore) and the helper re-sync restores the
            # missing links.
            commit_is_current = (
                getattr(coordinator, "last_successful_commit_id", None)
                == expected_context
            )
            gate = getattr(coordinator, "gate", None)
            gate_latched = bool(
                gate is not None and bool(getattr(gate, "failure_latched", False))
            )
            latch_reentry = bool(
                request.detail == "subwoofer-link-watcher"
                and commit_is_current
                and gate_latched
                and not bool(getattr(coordinator, "transition_active", False))
            )
            if not latch_reentry:
                return False
    elif _coordinator_commit_context_id() != expected_context or _playback_transition_is_active():
        return False

    if subwoofer_runtime is not None and subwoofer_runtime.sync_in_progress:
        logger.debug(
            "Coordinator recovery deferred while subwoofer runtime reconfiguration is in progress: reason=%s",
            request.detail,
        )
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
    """Run one transition under a monotonic attempt epoch."""
    global playback_transition_epoch, playback_transition_pending_attempts
    global playback_transition_coordinator, coordinator_last_successful_commit_id
    if playback_transition_coordinator is None:
        # Unit callers may invoke an endpoint without running FastAPI's
        # lifespan.  Production still initializes the same singleton during
        # startup; lazy construction keeps the ownership boundary identical.
        playback_transition_coordinator = PlaybackTransitionCoordinator(
            FxrouteTransitionRuntime(),
            gate_state_path=_playback_gate_state_path(),
        )
    attempt_epoch = _begin_playback_transition_attempt()
    request = replace(request, attempt_epoch=attempt_epoch)
    try:
        result = await playback_transition_coordinator.execute(request)
        if getattr(result, "committed", False):
            transition_id = getattr(result, "transition_id", None)
            if transition_id:
                coordinator_last_successful_commit_id = str(transition_id)
        return result
    finally:
        # The epoch changes before lock acquisition, so queued successors
        # invalidate older callbacks even while the current attempt drains.
        _end_playback_transition_attempt()


def _measurement_audio_graph_owned() -> bool:
    """Return whether the Measurement session currently owns audio routing."""
    session = measurement_sr_session
    return bool(session is not None and getattr(session, "owns_audio_graph", False))


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
    if _measurement_audio_graph_owned():
        logger.info(
            "Coordinator recovery skipped while Measurement owns the audio graph: reason=%s",
            reason,
        )
        return
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
    # Freeze the observation context before entering the Coordinator-owned
    # recovery slot.  A queued duplicate must retain the original context.
    attempt_commit_context_id = _coordinator_commit_context_id()
    if not attempt_commit_context_id:
        logger.info(
            "Coordinator recovery discarded without a committed context: reason=%s source=%s url=%s",
            reason,
            source,
            track.get("url") or track.get("id"),
        )
        return

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

    async def validate_recovery() -> bool:
        if _measurement_audio_graph_owned():
            logger.info(
                "Coordinator recovery skipped before execution while Measurement owns the audio graph: reason=%s",
                reason,
            )
            return False
        if not await _recovery_context_is_valid(request):
            logger.info(
                "Coordinator recovery discarded after context recheck: reason=%s source=%s url=%s commit_context=%s",
                reason,
                source,
                observed_url,
                attempt_commit_context_id,
            )
            return False
        if _measurement_audio_graph_owned():
            logger.info(
                "Coordinator recovery skipped at execution boundary while Measurement owns the audio graph: reason=%s",
                reason,
            )
            return False
        return True

    async def execute_recovery():
        try:
            result = await _run_coordinated_transition(request)
        except PlaybackTransitionFailure as exc:
            logger.warning("Coordinator recovery failed: %s", exc.as_status())
            return None
        if (
            getattr(result, "committed", False)
            and _sample_rate_policy_is_auto()
            and source in {"local", "radio"}
            and isinstance(result.target_rate, int)
            and result.target_rate > 0
        ):
            track["sample_rate_hz"] = result.target_rate
            if (
                current_track_info
                and current_track_info.get("source") == source
                and current_track_info.get("url") == track.get("url")
            ):
                current_track_info["sample_rate_hz"] = result.target_rate
        return result

    await playback_transition_coordinator.run_recovery(
        signature=signature,
        commit_context_id=attempt_commit_context_id,
        validate=validate_recovery,
        execute=execute_recovery,
    )


def _transition_error_http(exc: PlaybackTransitionFailure) -> HTTPException:
    return HTTPException(status_code=500, detail=exc.as_status())


def _mark_playback_intent_changed() -> None:
    """Advance the measurement-restore intent token after a user action."""
    global playback_intent_generation
    playback_intent_generation += 1


def _commit_coordinated_track(
    track_info: Mapping[str, Any],
    *,
    source: str,
    commit_token: str | None = None,
) -> None:
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
    # Publiziere den neuen Playback-Kontext-Token erst hier, nachdem alle
    # zum Kontext gehoerenden Globals committed wurden: der Ended-Waiter
    # kann zwischen Coordinator-Commit und dieser Boundary nie laufen (die
    # Boundary wird synchron im selben Caller-Step publiziert).
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
        self.queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=max_pending)
        self.ready = False
        self.failed = False
        self._task: "asyncio.Task | None" = None

    def enqueue(self, data: str) -> bool:
        """Queue one payload; False when the bounded queue is full."""
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            return False
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

    async def run(self) -> None:
        while True:
            data = await self.queue.get()
            try:
                if self.websocket.client_state.name != "CONNECTED":
                    raise RuntimeError("websocket is no longer CONNECTED")
                await asyncio.wait_for(
                    self.websocket.send_text(data), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                self.failed = True
                logger.debug(
                    f"WebSocket send timed out after {self.timeout:.1f}s"
                )
                return
            except Exception as exc:
                self.failed = True
                logger.debug(f"WebSocket send failed: {exc}")
                return
            finally:
                self.queue.task_done()


class ConnectionManager:
    """Fan-out broadcasts without letting one slow client stall the others.

    Every connected client owns one bounded delivery queue and exactly one
    send worker; all sends for a socket (broadcasts, init, pong) go through
    that worker, so per-client ordering is FIFO and concurrent ``send_text``
    calls are impossible.  A stuck client only delays its own queue; a
    timeout or send error removes the client, and a full queue is treated as
    an overloaded client that is disconnected instead of silently dropping
    events.
    """

    def __init__(self, send_timeout: float = 5.0, max_pending_sends: int = 8):
        self.active_connections: List[WebSocket] = []
        self._list_lock = asyncio.Lock()
        self._senders: dict[WebSocket, _ClientSender] = {}
        self._worker_tasks: set[asyncio.Task] = set()
        self._send_timeout = max(0.05, send_timeout)
        self._max_pending_sends = max(1, max_pending_sends)

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

    async def disconnect(self, websocket: WebSocket) -> bool:
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
        logger.info(f"WebSocket disconnected: {len(self.active_connections)} active")
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

    async def send_to_client(self, websocket: WebSocket, data: str) -> bool:
        """Queue one payload for a client's send worker.

        Returns False when the client is gone, its worker already failed, or
        its bounded queue is full; the full-queue case disconnects the client
        as too slow.
        """
        sender = self._senders.get(websocket)
        if sender is None or sender.failed:
            return False
        if not sender.enqueue(data):
            await self.disconnect(websocket)
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
                self.disconnect(websocket), name="ws-client-failure-cleanup"
            )
            self._worker_tasks.add(cleanup)
            cleanup.add_done_callback(self._worker_tasks.discard)

        task.add_done_callback(finished)

    async def broadcast(self, message: dict) -> None:
        data = json.dumps(message)
        async with self._list_lock:
            connections = list(self.active_connections)
        for connection in connections:
            if connection.client_state.name != "CONNECTED":
                await self.disconnect(connection)
                continue
            sender = self._senders.get(connection)
            if sender is None or not sender.ready:
                continue
            await self.send_to_client(connection, data)

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
    return playlist_io.track_relative_m3u_path(track, settings.MUSIC_ROOT)


def _build_m3u_for_playlist(playlist) -> str:
    """Thin wrapper: M3U export content lives in playlist_io (REFACTOR-002)."""
    return playlist_io.build_m3u_for_playlist(
        playlist,
        library_scanner.get_tracks(refresh=True),
        settings.MUSIC_ROOT,
    )


def _resolve_m3u_track_ids(entries: List[str], base_dir: Optional[Path] = None, tracks=None) -> List[str]:
    """Thin wrapper: M3U entry resolution lives in playlist_io (REFACTOR-002)."""
    if tracks is None:
        tracks = library_scanner.get_tracks(refresh=True)
    return playlist_io.resolve_m3u_track_ids(
        entries,
        settings.MUSIC_ROOT,
        base_dir=base_dir,
        tracks=tracks,
    )


def _import_m3u_playlist(name: str, content: str, base_dir: Optional[Path] = None, tracks=None) -> Optional[dict]:
    """Thin wrapper: M3U playlist import lives in playlist_io (REFACTOR-002)."""
    if tracks is None:
        tracks = library_scanner.get_tracks(refresh=True)
    return playlist_io.import_m3u_playlist(
        name,
        content,
        settings.MUSIC_ROOT,
        base_dir=base_dir,
        tracks=tracks,
    )


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
    global playback_queue, playback_queue_original, playback_queue_index, playback_queue_mode, playback_queue_loop, playback_queue_shuffle, single_track_loop
    was_native = playback_queue_mode == "native_mpv"
    if was_native:
        _reduce_native_mpv_playlist_to_current()
        _reset_mpv_loop_state()
    playback_queue = []
    playback_queue_original = []
    playback_queue_index = -1
    playback_queue_mode = "app_replace"
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


def _create_lifecycle_background_task(coro, *, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    lifecycle_background_tasks.add(task)
    task.add_done_callback(lifecycle_background_tasks.discard)
    return task


def _create_library_refresh_task(scanner: LibraryScanner, *, name: str) -> asyncio.Task:
    task = asyncio.create_task(asyncio.to_thread(scanner.refresh, True), name=name)
    library_refresh_tasks.add(task)
    task.add_done_callback(library_refresh_tasks.discard)
    return task


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
    try:
        live_volume = await asyncio.to_thread(get_output_volume)
    except Exception as exc:
        logger.warning(
            "Failed to read live output volume for silent-active check: %s", exc
        )
        live_volume = 100
    if live_volume <= 0:
        return

    overview = await asyncio.to_thread(get_audio_output_overview)
    output_mode = overview.get("output_mode") or {}
    links_result = await asyncio.to_thread(_run_debug_command, ["pw-link", "-l"], 2.0)
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
    """Return true only for a context token captured at an idle boundary.

    A token captured while any attempt was in flight is None (or an epoch
    that a newer attempt superseded) and can never become current again,
    exactly like a legacy odd-generation capture.
    """
    return (
        isinstance(generation, int)
        and generation == playback_transition_epoch
        and playback_transition_pending_attempts == 0
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
    """Allow only the known production-link drift during measurement.

    Subwoofer modes: EE->helper input-link drift.  Stereo: EE->hardware
    link drift with the EE output ports still present.
    """
    if diagnosis.get("links_complete"):
        return False
    if diagnosis.get("ee_ports") is not True:
        return False
    if diagnosis.get("measurement_rate_aligned") is not True:
        return False
    output_key = str(diagnosis.get("output_key") or "").strip()
    if not output_key:
        return False
    if diagnosis.get("mode") == OUTPUT_MODE_STEREO:
        missing = set(_missing_playback_graph_links(diagnosis))
        repairable = {
            f"ee_soe_output_level:output_FL -> {output_key}:playback_FL",
            f"ee_soe_output_level:output_FR -> {output_key}:playback_FR",
        }
        return bool(missing) and missing.issubset(repairable)
    if diagnosis.get("mode") not in OUTPUT_MODE_SUBWOOFER_MODES:
        return False
    if diagnosis.get("helper_ports") is not True:
        return False
    if diagnosis.get("helper_active") is not True:
        return False
    if diagnosis.get("helper_rate_matches") is not True:
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
        if initial.get("bypass_only"):
            # EasyEffects can recreate its direct EE -> hardware front links
            # after the output-mode preset reload, even though the helper
            # topology and the commit stage just reconciled them.  That is
            # the same invalid-but-link-only state the watcher heals via a
            # graph-only recovery; reconcile it here instead of failing the
            # committed transition over a second-generation bypass.
            await _coordinator_reconcile_subwoofer_links_only()
        else:
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
    ee_port_timeout_ms: int = PLAYBACK_HANDOFF_EE_PORT_TIMEOUT_MS,
) -> dict[str, Any]:
    """Build the effects/helper graph inside the Coordinator-owned gate.

    This is intentionally smaller than the removed legacy handoff.  Rate
    alignment belongs to ``establish_target_rate``; source loading belongs to
    the following adapter stages; this function only performs the idempotent
    EE/helper/link work and then uses the canonical graph readback.
    """
    global easyeffects_preset_load_lock

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
    compare_target_preset = None
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
            compare_target_preset = (
                compare.get("presetA") if active_side == "A" else
                compare.get("presetB") if active_side == "B" else
                None
            )
            current_preset = easyeffects_manager.get_active_preset()
            if compare_target_preset and current_preset != compare_target_preset:
                await _load_easyeffects_preset(compare_target_preset, convolver_sample_rate_hz=target_rate)
                needs_preset = True
                preset_reloaded = True
                logger.info(
                    "Coordinator output-mode switch loaded compare-active preset under gate: %s",
                    compare_target_preset,
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

        if request.operation in {"measurement-entry", "output-mode-switch"}:
            # The EE preset reload/rebuild above can leave the hardware sink
            # suspended at the configured default rate.  Re-establish the
            # target rate before the helper/stereo runtime sync, which defers
            # on any sink/authoritative rate mismatch.
            if not await _reconcile_transition_sink_rate(
                target_rate, reason=f"effects-{request.operation}"
            ):
                status = dict(get_samplerate_status())
                raise RuntimeError(
                    "Coordinator effects stage rate reconcile failed: "
                    f"expected={target_rate} active={status.get('active_rate')} "
                    f"force={status.get('force_rate')}"
                )

        if request.operation == "output-mode-switch":
            await _sync_subwoofer_runtime(
                audio_overview=overview,
                reason="coordinator-output-mode-switch",
                _rate_lock_held=False,
                target_overview=overview,
            )
            if easyeffects_manager is not None:
                if easyeffects_preset_load_lock is None:
                    easyeffects_preset_load_lock = asyncio.Lock()
                async with easyeffects_preset_load_lock:
                    # A/B can change while runtime sync recovers the graph, so
                    # use the current side rather than the pre-sync snapshot.
                    compare = easyeffects_manager.load_compare_state()
                    active_side = compare.get("activeSide") if compare.get("activeSide") in {"A", "B"} else None
                    compare_target_preset = (
                        compare.get("presetA") if active_side == "A" else
                        compare.get("presetB") if active_side == "B" else
                        None
                    )
                    if (
                        compare_target_preset
                        and easyeffects_manager.get_active_preset() != compare_target_preset
                    ):
                        # Stereo graph recovery may restart EasyEffects after
                        # the first reconciliation, resurrecting EE's own last
                        # preset and stale filter gain work point.
                        await _load_easyeffects_preset(
                            compare_target_preset,
                            convolver_sample_rate_hz=target_rate,
                        )
                        if not await _wait_for_easyeffects_output_ports(ee_port_timeout_ms):
                            raise RuntimeError(
                                "Coordinator compare preset restore did not recreate "
                                "EasyEffects output ports"
                            )
                        preset_reloaded = True
                        logger.info(
                            "Coordinator output-mode switch restored compare-active preset "
                            "after runtime sync: %s",
                            compare_target_preset,
                        )
            helper_rebuilt = mode in OUTPUT_MODE_SUBWOOFER_MODES
            if mode in OUTPUT_MODE_SUBWOOFER_MODES:
                await _coordinator_reconcile_subwoofer_links_only()
                links_reconciled = True
            else:
                # The direct EE -> hardware front links were restored by the
                # SUB-STOP above, but a concurrently taken pw-link snapshot
                # can transiently miss them (link readback racing the link
                # creation).  Repair the stereo links idempotently for every
                # stereo switch; the extra read also lets the listing settle
                # before the final diagnosis below.
                await _repair_stereo_output_links_once(diagnosis)
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
        # The production edges were just (re-)created by the SUB-STOP/helper
        # sync above, and EasyEffects can recreate its output nodes during
        # the transition, dropping the fresh links again.  A pw-link read
        # taken in such an unstable instant can transiently miss a single
        # edge (observed live: one missing helper input link right after the
        # sync verified the full graph).  Repair idempotently per mode and
        # re-read after a short settle before declaring the switch failed.
        for _ in range(3):
            if mode not in OUTPUT_MODE_SUBWOOFER_MODES:
                await _repair_stereo_output_links_once(final)
            elif subwoofer_runtime is not None:
                await _coordinator_reconcile_subwoofer_links_only()
            await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS * 5 / 1000)
            final = await _playback_graph_diagnosis(
                overview,
                target_rate=target_rate,
                require_source=False,
            )
            if final.get("links_complete"):
                break
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
    return not (
        isinstance(intent_generation, int)
        and intent_generation != playback_intent_generation
    )


def _local_intent_matches_live_state(
    *,
    expected_source: str,
    expected_id: Any,
    expected_url: str | None,
    expected_file: str | None,
    intent_generation: Any,
) -> bool:
    """Return whether the live MPV/local context still matches a captured intent."""
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
    if expected_file and current_file != expected_file:
        return False
    if (
        isinstance(intent_generation, int)
        and intent_generation != playback_intent_generation
    ):
        return False
    return True





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
    _create_lifecycle_background_task(
        _repair_subwoofer_runtime_inputs_after_measurement_release(target_rate),
        name="measurement-release-input-repair",
    )


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
    ) or _measurement_audio_graph_owned():
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
    track_rate = _coordinator_source_rate(source, track)
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

    target_rate = samplerate.effective_playback_rate(actual_rate)
    if not isinstance(target_rate, int) or target_rate <= 0:
        _reset_samplerate_drift_observation()
        return

    source_metadata_aligned = (
        samplerate.load_sample_rate_policy().get("mode") == "fixed"
        or track_rate == actual_rate
    )
    healthy = (
        source_metadata_aligned
        and active_rate == target_rate
        and (force_rate is None or force_rate == 0 or force_rate == target_rate)
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
        target_rate,
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
            f"track={track_rate}:mpv={actual_rate}:target={target_rate}:active={active_rate}:force={force_rate}"
        ),
        "expected_rate": target_rate,
        "track_rate": track_rate,
        "actual_rate": actual_rate,
        "mpv_rate": actual_rate,
        "hardware_rate": active_rate,
        "force_rate": force_rate,
    }
    _reset_samplerate_drift_observation()
    logger.warning(
        "Stable playback samplerate drift observed; requesting Coordinator recovery: "
        "source=%s url=%s track=%s mpv=%s target=%s active=%s force=%s",
        source,
        expected_url,
        track_rate,
        actual_rate,
        target_rate,
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
            if _measurement_audio_graph_owned():
                logger.debug("Subwoofer link watcher skipped while Measurement owns the audio graph")
                continue
            await _observe_playback_samplerate_drift()
            if subwoofer_runtime is None:
                continue
            overview = get_audio_output_overview()
            output_mode = overview.get("output_mode") or {}
            if output_mode.get("mode") not in OUTPUT_MODE_SUBWOOFER_MODES:
                continue
            if _playback_transition_is_active():
                continue
            if subwoofer_runtime.sync_in_progress:
                logger.debug(
                    "Subwoofer link watcher skipped while a subwoofer runtime reconfiguration is in progress"
                )
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
        if transition_generation != playback_transition_epoch:
            logger.info(
                "Radio post-load rate wait aborted: stale transition "
                "generation=%s current=%s",
                transition_generation,
                playback_transition_epoch,
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
    await _load_easyeffects_preset(active_preset, convolver_sample_rate_hz=sample_rate_hz)
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


@dataclass
class _QueueCandidate:
    """Prepared queue state that is not yet committed to the globals.

    ``/api/play`` builds one of these, hands the immutable snapshot to the
    Coordinator, and publishes it only after the playback transition
    committed.  The committed globals stay untouched until then.
    """

    queue: list
    original: list
    index: int
    mode: str
    loop: bool
    shuffle: bool
    single_track_loop: bool
    track: dict


def _cleared_queue_candidate(track: dict | None = None) -> _QueueCandidate:
    """Candidate for a play request that intentionally replaces any queue."""
    return _QueueCandidate(
        queue=[],
        original=[],
        index=-1,
        mode="app_replace",
        loop=False,
        shuffle=False,
        single_track_loop=False,
        track=dict(track) if track else {},
    )


def _commit_queue_state(candidate: _QueueCandidate) -> None:
    """Publish a prepared queue candidate to the committed globals."""
    global playback_queue, playback_queue_original, playback_queue_index
    global playback_queue_mode
    global playback_queue_loop, playback_queue_shuffle, single_track_loop
    playback_queue = [dict(item) for item in candidate.queue]
    playback_queue_original = [dict(item) for item in candidate.original]
    playback_queue_index = candidate.index
    playback_queue_mode = candidate.mode
    playback_queue_loop = candidate.loop
    playback_queue_shuffle = candidate.shuffle
    single_track_loop = candidate.single_track_loop


def _native_queue_request_fields(queue_state: _QueueCandidate | None = None) -> dict[str, Any]:
    """Snapshot native-queue metadata for a Coordinator request.

    Reads the committed globals by default; callers staging a not-yet-
    committed queue state pass that candidate instead.
    """
    if queue_state is None:
        mode = playback_queue_mode
        queue = playback_queue
        index = playback_queue_index
        loop = playback_queue_loop
    else:
        mode = queue_state.mode
        queue = queue_state.queue
        index = queue_state.index
        loop = queue_state.loop
    if mode != "native_mpv" or not _can_use_native_local_queue(queue):
        return {}
    start_index = index if index >= 0 else 0
    return {
        "native_queue": tuple(dict(item) for item in queue),
        "native_queue_index": start_index,
        "native_queue_loop": bool(loop),
        # Queue order is already concrete in the prepared state.  Keep the
        # field explicit for request compatibility, but never ask MPV to
        # reshuffle it.
        "native_queue_shuffle": False,
    }


def _shuffled_around_current(tracks: list[dict], current_track: dict | None) -> list[dict]:
    """Return dict copies with the current track first and the rest shuffled.

    The current track is identified by its id so duplicate copies of the same
    track stay together at the front.  With ``current_track`` omitted, only
    the shuffled copies are returned.
    """
    current = dict(current_track) if current_track is not None else None
    remaining = [
        dict(track)
        for track in tracks
        if current is None or track.get("id") != current.get("id")
    ]
    random.shuffle(remaining)
    if current is None:
        return remaining
    return [current] + remaining


def _prepare_local_queue(track_id: str, queue_track_ids: Optional[list[str]] = None, shuffle: bool = False, loop: bool = False, *, reshuffle: bool = True, tracks: Optional[list] = None) -> _QueueCandidate:
    """Build the requested queue as an uncommitted candidate.

    Metadata-only preparation: the committed queue globals are untouched
    until ``_commit_queue_state`` publishes the candidate after a successful
    playback transition.

    ``tracks`` may be passed in from async callers that already offloaded
    the scan-capable ``library_scanner.get_tracks()`` read to a worker.
    """
    if tracks is None:
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
        ordered_tracks = _shuffled_around_current(ordered_tracks, current_track)

    queue = ordered_tracks if len(ordered_tracks) > 1 else []
    original = original_tracks if len(original_tracks) > 1 else []
    # A homogeneous local queue is safe to hand to MPV only after the
    # Coordinator has committed the common rate/DSP/graph/gate state.  The
    # request carries the immutable queue snapshot; the mode becomes visible
    # as native only after that transition commits.
    mode = "native_mpv" if _can_use_native_local_queue(ordered_tracks) else "app_replace"
    if queue:
        track_index = next(
            (index for index, item in enumerate(queue) if item.get("id") == track_id),
            0,
        )
        track = dict(queue[track_index])
    else:
        track_index = -1
        track = dict(ordered_tracks[0])

    return _QueueCandidate(
        queue=queue,
        original=original,
        index=track_index,
        mode=mode,
        loop=bool(loop and len(ordered_tracks) > 1),
        shuffle=bool(shuffle and len(ordered_tracks) > 1),
        single_track_loop=bool(loop and len(ordered_tracks) == 1),
        track=track,
    )




async def _load_queue_track(index: int, *, transition_reason: str = "queue navigation", queue_candidate: _QueueCandidate | None = None) -> bool:
    """Navigate to ``index`` of the committed queue.

    ``queue_candidate`` allows queue navigation that first replaces the
    committed queue (manual shuffle wrap): the prepared queue is published
    only after the transition committed, and failure keeps the old
    order/index/track context intact.
    """
    global playback_queue_index
    if queue_candidate is not None:
        if index < 0 or index >= len(queue_candidate.queue):
            return False
        next_track = dict(queue_candidate.queue[index])
        native_fields = {}
        native_jump = False
    else:
        if len(playback_queue) <= 1 or index < 0 or index >= len(playback_queue):
            return False
        next_track = dict(playback_queue[index])
        native_fields = _native_queue_request_fields()
        native_jump = playback_queue_mode == "native_mpv"
    target_url = str(next_track.get("url") or "")
    if not target_url:
        _clear_playback_queue()
        return False
    source = str(next_track.get("source") or "local")
    target_rate = _coordinator_target_rate(source, next_track)
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
    if not getattr(result, "committed", False):
        raise HTTPException(status_code=500, detail="Playback transition was not committed")
    rate_updated = False
    if _sample_rate_policy_is_auto() and source in {"local", "radio"} and isinstance(result.target_rate, int) and result.target_rate > 0:
        next_track["sample_rate_hz"] = result.target_rate
        rate_updated = True
    if queue_candidate is not None:
        _commit_queue_state(queue_candidate)
    if rate_updated and 0 <= index < len(playback_queue):
        playback_queue[index]["sample_rate_hz"] = result.target_rate
    if queue_candidate is None:
        playback_queue_index = index
    _commit_coordinated_track(
        next_track, source=source, commit_token=getattr(result, "transition_id", None)
    )
    return True


async def _advance_playback_queue(*, transition_reason: str = "queue advance") -> str:
    """Advance the committed queue.

    Returns an explicit tri-state outcome instead of a plain bool so the
    API can distinguish a successful terminal queue end from a navigation
    that was not possible at all:

    ``"advanced"``
        Another queue track was committed successfully.
    ``"ended"``
        The terminal queue end state was committed successfully (the
        queue is cleared, matching the auto-EOF end semantics).  The
        queue mutation happened before this value is returned; the API
        must represent it as a successful response, never as an error.
    ``"unavailable"``
        No navigation was possible and no authoritative queue state was
        changed.
    """
    global playback_queue_index
    if len(playback_queue) <= 1:
        return "unavailable"
    next_index = playback_queue_index + 1
    if next_index >= len(playback_queue):
        if playback_queue_mode == "native_mpv":
            if playback_queue_loop:
                next_index = 0
            else:
                return "unavailable"
        else:
            manual_shuffle_wrap = playback_queue_shuffle and transition_reason.startswith("manual queue next")
            if playback_queue_loop or manual_shuffle_wrap:
                if playback_queue_shuffle:
                    current_index = playback_queue_index if 0 <= playback_queue_index < len(playback_queue) else 0
                    current_track = dict(playback_queue[current_index])
                    # Prepare the reshuffled wrap without touching the
                    # committed queue: it is published only after the
                    # navigation transition committed.
                    shuffled = _shuffled_around_current(playback_queue, current_track)
                    next_index = 1 if len(shuffled) > 1 else 0
                    candidate = _QueueCandidate(
                        queue=shuffled,
                        original=[dict(track) for track in playback_queue_original],
                        index=next_index,
                        mode="app_replace",
                        loop=playback_queue_loop,
                        shuffle=True,
                        single_track_loop=False,
                        track=dict(shuffled[next_index]),
                    )
                    return (
                        "advanced"
                        if await _load_queue_track(
                            next_index,
                            transition_reason=transition_reason,
                            queue_candidate=candidate,
                        )
                        else "unavailable"
                    )
                else:
                    next_index = 0
            else:
                _clear_playback_queue()
                return "ended"
    return (
        "advanced"
        if await _load_queue_track(next_index, transition_reason=transition_reason)
        else "unavailable"
    )


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
        target_queue = _shuffled_around_current(playback_queue, current_track)
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
        if not getattr(result, "committed", False):
            raise HTTPException(status_code=500, detail="Playback transition was not committed")

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


def _sync_active_local_queue_selection(queue_track_ids: Optional[list[str]] = None, shuffle: bool = False, loop: bool = False, *, tracks: Optional[list] = None) -> dict:
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

    candidate = _prepare_local_queue(
        current_track["id"],
        queue_track_ids,
        shuffle=shuffle,
        loop=loop,
        reshuffle=False,
        tracks=tracks,
    )
    _commit_queue_state(candidate)
    track_info = candidate.track
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
    return get_status_volume(default)


async def _set_canonical_output_volume(volume: float | int) -> dict[str, Any]:
    """Apply the one UI-volume contract for local, radio and Spotify.

    Loudness owns the attenuation when enabled, therefore the physical system
    master remains at 100%.  Without Loudness the existing FXRoute system
    volume curve remains authoritative.

    The whole write is serialized against other canonical volume writes so
    set -> verified get -> cache publish sequences never interleave.  The
    loudness.enabled decision and the Loudness read-modify-write both run
    under the central EasyEffects mutation ownership, so no other EE mutation
    can interleave between the extras read and the write.  Blocking manager
    and wpctl operations run off the event loop via cancellation-safe
    workers; the caller owns the locks until the workers actually finished.
    Lock order: canonical volume write lock first, then EasyEffects mutation
    lock.
    """
    async with _canonical_volume_write_lock():
        requested = max(0, min(100, int(round(float(volume)))))
        async with _easyeffects_mutation_lock():
            extras = easyeffects_manager.load_global_extras() if easyeffects_manager else {}
            loudness = extras.get("loudness") if isinstance(extras, dict) else {}
            if isinstance(loudness, dict) and loudness.get("enabled") and easyeffects_manager:
                volume_db = easyeffects_manager.loudness_db_from_percent(requested)
                volume_result = await _drain_worker(
                    easyeffects_manager.set_loudness_volume_db, volume_db
                )
                await _drain_worker(set_output_volume, 100)
                return {
                    "volume": requested,
                    "loudnessVolumeDb": float(
                        volume_result["extras"]["loudness"]["params"]["volumeDb"]
                    ),
                    "loudness_enabled": True,
                }
        return {
            "volume": await _drain_worker(set_output_volume, requested),
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
        if transition_status.get("transition_blocked"):
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
        transition_generation = _capture_playback_transition_epoch()
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
            if _is_local_playback_active(refreshed_player_state):
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
    is_local_playing = _is_local_playback_active(player_state)
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
    _create_lifecycle_background_task(
        _run_peak_monitor_refresh_after_effects_change(reason),
        name=f"peak-monitor-refresh:{reason}",
    )



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
            _capture_playback_transition_epoch(),
        )
    )


# Callback functions
def _mark_player_state_authoritative(state: dict | None) -> None:
    global latest_player_state_seq_seen
    seq = (state or {}).get("_seq")
    if isinstance(seq, int):
        latest_player_state_seq_seen = max(latest_player_state_seq_seen, seq)


def _dispatch_player_state_change(state: dict):
    """Synchronous player-state dispatcher.

    player.py invokes this at notify time (before any coroutine task is
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
    global queue_advancing, playback_queue_index, current_track_info, last_track_info, latest_player_state_seq_seen
    callback_generation = _capture_playback_transition_epoch()
    seq = state.get("_seq")
    if isinstance(seq, int):
        if seq < latest_player_state_seq_seen:
            return
        latest_player_state_seq_seen = seq

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
        and state.get("ended")
        and not state.get("current_file")
        and current_track_info
        and current_track_info.get("source") == "local"
        and playback_queue_mode != "native_mpv"
    ):
        queue_advancing = True
        try:
            if len(playback_queue) > 1 and await _advance_playback_queue(transition_reason="queue auto-advance") == "advanced":
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
                    if _sample_rate_policy_is_auto() and isinstance(result.target_rate, int) and result.target_rate > 0:
                        current_track_info["sample_rate_hz"] = result.target_rate
                    # Eine neue physische Playback-Instanz wurde committed:
                    # publiziere ausschliesslich den Playback-Instance-Token
                    # (kein _commit_coordinated_track: dessen Side Effects wie
                    # Intent/History sind fuer automatisches Single-Track-Loop
                    # unerwuenscht).
                    _publish_playback_context_commit(getattr(result, "transition_id", None))
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
            playback_transition_epoch,
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
            await _drain_worker(library_scanner.refresh, True, wait_if_running=True)
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
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_PACTL_COMMAND_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        # A hanging PulseAudio/PipeWire daemon must not block a source-mode
        # change (startup or /api/audio/source-mode) forever.  Report the
        # timeout as a controlled command failure, exactly like the
        # nonzero-exit path below.  If the caller is cancelled while the
        # cleanup drains, the cancellation wins over the timeout failure.
        if await _stop_command_child_cancellation_safe(
            proc, _PACTL_TERMINATE_GRACE_SECONDS
        ):
            raise asyncio.CancelledError
        raise RuntimeError(
            f"pactl {' '.join(args)} timed out after {_PACTL_COMMAND_TIMEOUT_SECONDS}s"
        )
    except asyncio.CancelledError:
        # Caller cancellation must not leave the child behind; the shielded
        # cleanup terminates/kills/drains it even under further cancellation,
        # then the original cancellation is re-raised.
        await _stop_command_child_cancellation_safe(proc, _PACTL_TERMINATE_GRACE_SECONDS)
        raise
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="ignore").strip() or f"pactl {' '.join(args)} failed")
    return stdout.decode(errors="ignore").strip()


_PACTL_COMMAND_TIMEOUT_SECONDS = 3
_PACTL_TERMINATE_GRACE_SECONDS = 1
_PW_LINK_COMMAND_TIMEOUT_SECONDS = 10
_PW_LINK_TERMINATE_GRACE_SECONDS = 3


async def _stop_command_child(proc, grace_seconds: float) -> None:
    """Terminate a still-running command child and drain it terminally.

    terminate -> bounded communicate() (drains stdout+stderr) -> if the
    child ignores SIGTERM: kill -> bounded communicate().  Process and both
    pipes are thereby always worked off terminally; a final wait() guards
    against a pathological case where even the killed child's pipes never
    close.  Already-exited processes are handled cheaply.
    """
    if proc is None or proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.communicate(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.communicate(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            await proc.wait()


async def _stop_command_child_cancellation_safe(proc, grace_seconds: float) -> bool:
    """Stop and drain a command child shielded from caller cancellation.

    Runs the actual stop in its own task behind ``asyncio.shield``: even a
    second cancellation during the grace period cannot interrupt the
    terminate/grace/kill/pipe-drain sequence, so no child can be orphaned by
    caller cancellation.  Returns True when the caller was cancelled while
    draining; the caller must then propagate CancelledError (it wins over
    any timeout failure).  Cleanup errors are best-effort and swallowed.
    """
    if proc is None or proc.returncode is not None:
        return False
    cleanup_task = asyncio.create_task(_stop_command_child(proc, grace_seconds))
    cancelled = False
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        cleanup_task.result()
    except Exception:
        logger.debug("FXRoute command child cleanup failed", exc_info=True)
    return cancelled


async def _stop_pw_link_process(proc) -> None:
    """Terminate and fully reap a pw-link child (no zombie/pipe left behind)."""
    await _stop_command_child(proc, _PW_LINK_TERMINATE_GRACE_SECONDS)


async def _stop_pactl_process(proc) -> None:
    """Terminate and fully reap a pactl child (no zombie/pipe left behind)."""
    await _stop_command_child(proc, _PACTL_TERMINATE_GRACE_SECONDS)


async def _run_pw_link_command(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "pw-link", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_PW_LINK_COMMAND_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        # A hanging PipeWire registry must not block a request forever.
        # Report the timeout as a controlled command failure, exactly like
        # the nonzero-exit path below.
        await _stop_pw_link_process(proc)
        raise RuntimeError(
            f"pw-link {' '.join(args)} timed out after {_PW_LINK_COMMAND_TIMEOUT_SECONDS}s"
        )
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
    try:
        for channel in ("FL", "FR"):
            source_port = f"{normalized}:capture_{channel}"
            sink_port = f"easyeffects_sink:playback_{channel}"
            await _connect_ports((source_port,), sink_port)
    except BaseException:
        await _disconnect_external_input_source(normalized)
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
    if not proc:
        return
    try:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    finally:
        if bluetooth_agent_process is proc:
            bluetooth_agent_process = None


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
    try:
        await asyncio.sleep(0.4)
    except BaseException:
        await _stop_bluetooth_audio_agent()
        raise
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
    try:
        await _link_bluetooth_source_to_easyeffects(normalized)
    except BaseException:
        await _disconnect_bluetooth_input_source(normalized)
        raise
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
        result = await _run_locked_worker(
            _easyeffects_mutation_lock(),
            easyeffects_manager.ensure_stereo_output_graph,
            output_key,
        )
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
                target_rate = samplerate.effective_playback_rate(canonical_rate)
                if spotify_rate != canonical_rate or sink_rate != target_rate:
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
                        "expected_rate": target_rate,
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
    global settings, player_instance, library_scanner, library_scan_task, downloader, easyeffects_manager, measurement_store, measurement_sr_session, measurement_watchdog_task, peak_monitor, subwoofer_runtime, subwoofer_runtime_link_watch_task, hardware_controller, peak_monitor_playback_armed, peak_monitor_transition_lock, peak_monitor_context_signature, easyeffects_preset_load_lock, easyeffects_mutation_lock, canonical_volume_write_lock, source_transition_lock, playback_transition_coordinator, coordinator_last_successful_commit_id, external_input_loopback_module_id, external_input_loopback_source_name, bluetooth_input_source_name, bluetooth_monitor_task, bluetooth_agent_process, spotify_playerctl_watch_task, spotify_playerctl_detect_task, spotify_state_refresh_task, spotify_state_poll_task, spotify_playerctl_last_trigger_at, current_source_mode, latest_spotify_state, radio_reconnect_task

    logger.info("Starting FXRoute... build_id=%s", _read_build_id())
    try:
        settings = get_settings()
        logger.info("Configuration loaded. MUSIC_ROOT: %s", settings.MUSIC_ROOT)
        logger.info("Download directory: %s", settings.download_dir)

        player_instance = get_player()
        try:
            await _drain_worker(player_instance.start)
            logger.info("MPV player started")
            ensure_local_source_volume()
        except MPVNotInstalledError as exc:
            logger.error("Failed to start MPV: %s", exc)

        library_scanner = LibraryScanner()
        library_scanner.prepare_scan_status()
        library_scan_task = _create_library_refresh_task(
            library_scanner,
            name="initial-library-scan",
        )
        logger.info("Library scanner initialized; initial scan running in background")

        downloader = Downloader()
        logger.info("Downloader initialized")

        easyeffects_manager = await _drain_worker(EasyEffectsManager)
        if easyeffects_manager.load_global_extras().get("loudness", {}).get("enabled"):
            set_output_volume(100)
        volume_read_monitor_task = start_volume_read_monitor()
        lifecycle_background_tasks.add(volume_read_monitor_task)
        volume_read_monitor_task.add_done_callback(lifecycle_background_tasks.discard)
        logger.info("EasyEffects manager initialized")

        measurement_store = MeasurementStore()
        logger.info("Measurement store initialized: %s", measurement_store.measurements_dir)
        measurement_sr_session = MeasurementSampleRateSession()
        measurement_watchdog_task = asyncio.create_task(
            measurement_sr_session.run_watchdog(),
            name="measurement-session-watchdog",
        )
        logger.info("Measurement sample-rate session initialized")

        playback_transition_coordinator = PlaybackTransitionCoordinator(
            FxrouteTransitionRuntime(),
            gate_state_path=_playback_gate_state_path(),
        )
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
            policy = samplerate.load_sample_rate_policy()
            if policy.get("mode") == "fixed":
                try:
                    await _transition_sample_rate_policy(policy, detail="startup-sample-rate-policy")
                    logger.info("Re-applied fixed sample-rate policy: %s Hz", policy.get("rate"))
                except Exception as exc:
                    logger.warning("Failed to re-apply fixed sample-rate policy: %s", exc)
            await _sync_subwoofer_runtime(applied_output or get_audio_output_overview())
            subwoofer_runtime_link_watch_task = asyncio.create_task(
                _subwoofer_runtime_link_watch_loop(),
                name="subwoofer-runtime-link-watch",
            )
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

        bluetooth_monitor_task = asyncio.create_task(
            _bluetooth_input_monitor_loop(),
            name="bluetooth-input-monitor",
        )
        spotify_playerctl_last_trigger_at = 0.0
        logger.info("Starting Spotify playerctl watch task")
        spotify_playerctl_watch_task = asyncio.create_task(
            _spotify_playerctl_watch_loop(),
            name="spotify-playerctl-watch",
        )
        logger.info("Starting Spotify metadata poll fallback task")
        spotify_state_poll_task = asyncio.create_task(
            _spotify_state_poll_loop(),
            name="spotify-state-poll",
        )

        player_instance.register_callbacks(_dispatch_player_state_change)
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
    global settings, player_instance, library_scanner, library_scan_task, downloader, easyeffects_manager, measurement_store, measurement_sr_session, measurement_watchdog_task, peak_monitor, subwoofer_runtime, subwoofer_runtime_link_watch_task, hardware_controller, peak_monitor_transition_lock, peak_monitor_context_signature, easyeffects_preset_load_lock, easyeffects_mutation_lock, canonical_volume_write_lock, source_transition_lock, playback_transition_coordinator, external_input_loopback_module_id, external_input_loopback_source_name, bluetooth_input_source_name, bluetooth_monitor_task, bluetooth_agent_process, spotify_playerctl_watch_task, spotify_playerctl_detect_task, spotify_state_refresh_task, spotify_state_poll_task, radio_reconnect_task

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
        subwoofer_runtime_link_watch_task,
        measurement_watchdog_task,
        bluetooth_monitor_task,
        spotify_playerctl_watch_task,
        spotify_playerctl_detect_task,
        spotify_state_refresh_task,
        spotify_state_poll_task,
        radio_reconnect_task,
        *silent_active_watch_tasks.values(),
        *lifecycle_background_tasks,
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
    silent_active_watch_tasks.clear()
    lifecycle_background_tasks.clear()

    if player_instance is not None:
        await cleanup(
            "player-callbacks",
            lambda: player_instance.shutdown_callbacks(_dispatch_player_state_change),
        )
    await cleanup("autosub", autosub.shutdown)
    if measurement_store is not None:
        await cleanup("measurement-store", measurement_store.shutdown)
    await cleanup("spl-calibration", spl_calibration.shutdown)
    if measurement_sr_session is not None:
        await cleanup("measurement-session", measurement_sr_session.request_close)
    late_background_tasks = [task for task in lifecycle_background_tasks if not task.done()]
    for task in late_background_tasks:
        task.cancel()
    if late_background_tasks:
        await cleanup(
            "late-background-tasks",
            lambda: asyncio.gather(*late_background_tasks, return_exceptions=True),
        )
    lifecycle_background_tasks.clear()
    if downloader is not None:
        await cleanup("downloader", lambda: asyncio.to_thread(downloader.shutdown))
    if library_scanner is not None:
        library_scanner.cancel_refresh()
    refresh_tasks = [task for task in library_refresh_tasks if not task.done()]
    if refresh_tasks:
        await cleanup(
            "library-refresh-tasks",
            lambda: asyncio.gather(*refresh_tasks, return_exceptions=True),
        )
    library_refresh_tasks.clear()
    if player_instance is not None:
        await cleanup("player", lambda: asyncio.to_thread(player_instance.stop))
    if subwoofer_runtime is not None:
        await cleanup("subwoofer-runtime", subwoofer_runtime.stop)
    if bluetooth_agent_process is not None or bluetooth_input_source_name is not None:
        await cleanup("bluetooth-input", _disable_bluetooth_input_monitoring)
    await cleanup("bluetooth-receiver", lambda: asyncio.to_thread(set_bluetooth_receiver_enabled, False))
    if external_input_loopback_module_id is not None or external_input_loopback_source_name is not None:
        await cleanup("external-input", _disable_external_input_loopback)
    if peak_monitor is not None:
        await cleanup("peak-monitor", peak_monitor.stop)
    if hardware_controller is not None:
        await cleanup("hardware-controller", lambda: asyncio.to_thread(hardware_controller.close))

    settings = None
    player_instance = None
    library_scanner = None
    library_scan_task = None
    downloader = None
    easyeffects_manager = None
    measurement_store = None
    measurement_sr_session = None
    measurement_watchdog_task = None
    peak_monitor = None
    subwoofer_runtime = None
    subwoofer_runtime_link_watch_task = None
    hardware_controller = None
    peak_monitor_transition_lock = None
    peak_monitor_context_signature = None
    easyeffects_preset_load_lock = None
    easyeffects_mutation_lock = None
    canonical_volume_write_lock = None
    source_transition_lock = None
    playback_transition_coordinator = None
    external_input_loopback_module_id = None
    external_input_loopback_source_name = None
    bluetooth_input_source_name = None
    bluetooth_monitor_task = None
    bluetooth_agent_process = None
    spotify_playerctl_watch_task = None
    spotify_playerctl_detect_task = None
    spotify_state_refresh_task = None
    spotify_state_poll_task = None
    radio_reconnect_task = None
    if cleanup_cancelled:
        raise asyncio.CancelledError

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

    previous_state = dict(player_instance.state)
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
        queue_candidate = _cleared_queue_candidate(track_info)
    else:
        active_queue_ids = [item.get("id") for item in playback_queue]
        preserve_queue_order = bool(req.queue_track_ids) and list(req.queue_track_ids) == active_queue_ids
        tracks = await _drain_worker(library_scanner.get_tracks) if library_scanner is not None else None
        queue_candidate = _prepare_local_queue(
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
    native_queue_fields = _native_queue_request_fields(queue_candidate) if source == "local" else {}
    native_trim_required = playback_queue_mode == "native_mpv" and queue_candidate.mode != "native_mpv"
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
            _reduce_native_mpv_playlist_to_current()
            _reset_mpv_loop_state()
        except Exception:
            logger.warning(
                "Failed to trim native playlist after committed play transition",
                exc_info=True,
            )
    if source in {"local", "radio"} and isinstance(result.target_rate, int) and result.target_rate > 0:
        track_info["sample_rate_hz"] = result.target_rate

    _commit_queue_state(queue_candidate)
    _commit_coordinated_track(
        track_info, source=source, commit_token=getattr(result, "transition_id", None)
    )
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
    if _playback_transition_is_active():
        raise HTTPException(status_code=409, detail="A playback transition is in progress")

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
    if _playback_transition_is_active():
        raise HTTPException(status_code=409, detail="A playback transition is in progress")
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
            if _sample_rate_policy_is_auto() and source in {"local", "radio"} and isinstance(result.target_rate, int) and result.target_rate > 0:
                active_track["sample_rate_hz"] = result.target_rate
            _commit_coordinated_track(
                active_track, source=source, commit_token=getattr(result, "transition_id", None)
            )
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
    if _sample_rate_policy_is_auto() and source in {"local", "radio"} and isinstance(result.target_rate, int) and result.target_rate > 0:
        replay_track["sample_rate_hz"] = result.target_rate
    _commit_coordinated_track(
        replay_track, source=source, commit_token=getattr(result, "transition_id", None)
    )
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
    if _playback_transition_is_active():
        raise HTTPException(status_code=409, detail="A playback transition is in progress")
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
        volume_result = await _set_canonical_output_volume(vol)
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
    result = await _advance_playback_queue(transition_reason="manual queue next")
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
            "playback": build_playback_payload(player_instance.state),
        }
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

    tracks = await _drain_worker(library_scanner.get_tracks) if library_scanner is not None else None
    playback = _sync_active_local_queue_selection(
        queue_track_ids=queue_track_ids,
        shuffle=bool(body.get("shuffle", False)),
        loop=bool(body.get("loop", False)),
        tracks=tracks,
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
    if _playback_transition_is_active():
        raise HTTPException(status_code=409, detail="A playback transition is in progress")
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
    # Re-check after the last await: a transition may have started while the
    # request body was being read.  Never seek or mark intent mid-transition.
    if _playback_transition_is_active():
        raise HTTPException(status_code=409, detail="A playback transition is in progress")
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
    status = await asyncio.to_thread(get_samplerate_status)
    logger.info(
        "audio_samplerate_status entry: footer_owner=%s active_rate=%s sink_state=%s",
        current_footer_owner,
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
    overview = _with_subwoofer_derived_delays(await asyncio.to_thread(get_audio_output_overview))
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

    if measurement_sr_session is not None and measurement_sr_session.has_active_jobs:
        raise HTTPException(status_code=423, detail="Measurement is active; output mode switch is locked")

    try:
        target = prepare_audio_output_mode(mode, subwoofer, subwoofers)
        target_mode = str(target["config"].get("mode") or "").strip()

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
            result = persist_audio_output_mode(target["config"])
            await _sync_subwoofer_runtime(result, reason="output-mode-params")
            result = _with_subwoofer_derived_delays(result)
            if subwoofer_runtime is not None:
                result["output_mode"] = {
                    **(result.get("output_mode") or {}),
                    "runtime": subwoofer_runtime.snapshot(),
                }
            await refresh_peak_monitor_after_effects_change("audio-output-mode-params")
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


async def _load_easyeffects_preset(
    preset_name: str, *, convolver_sample_rate_hz: int | None = None
) -> None:
    """Serialize preset loads against threaded EasyEffects mutations.

    load_preset() also synchronizes global extras into the preset and is
    therefore not read-only: it must never run concurrently with a threaded
    IR/preset mutation.  Lock order: the mutation lock is always acquired
    after (never before) any preset-load lock held by the caller.  The
    blocking manager call runs off the event loop under the caller-owned
    mutation lock (no double acquisition).
    """
    async with _easyeffects_mutation_lock():
        manager = _require_easyeffects_manager()
        await _drain_worker(
            manager.load_preset,
            preset_name,
            convolver_sample_rate_hz=convolver_sample_rate_hz,
        )


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
        await _load_easyeffects_preset(preset_name)
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

    # A Loudness enabled-state transition owns the canonical volume write
    # lock from the live read through the Loudness mutation and the final
    # master=100, so a parallel /api/volume or /api/spotify/volume request
    # can never interleave.  All transferred values are (re)read under the
    # lock.  Non-Loudness extras updates never take this lock.
    #
    # The full read-modify-write (extras read, JSON merge, resolution, manager
    # mutation/persistence) runs under the central EasyEffects mutation
    # ownership so a parallel coordinator/volume/SPL mutation can never
    # interleave between the read and the write.  Lock order: canonical
    # volume write lock first, then EasyEffects mutation lock.
    canonical_transition = any(
        key in body for key in ("loudness_enabled", "loudnessEnabled")
    )
    canonical_lock = None
    if canonical_transition:
        canonical_lock = _canonical_volume_write_lock()
        await canonical_lock.acquire()
    try:
        async with _easyeffects_mutation_lock():
            previous = ee_manager.load_global_extras()
            parsed = _merge_effects_extras_from_json(previous, body)
            was_loudness = bool(previous.get("loudness", {}).get("enabled"))
            enabling_loudness = bool(parsed.get("loudness", {}).get("enabled")) and not was_loudness
            disabling_loudness = was_loudness and not bool(parsed.get("loudness", {}).get("enabled"))
            if enabling_loudness:
                raw_volume = await _drain_worker(get_output_volume)
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
                await _drain_worker(set_output_volume, disabling_master_percent)
            try:
                if runtime_strength_change:
                    result = await _drain_worker(
                        ee_manager.apply_loudness_strength_runtime, previous, extras
                    )
                elif runtime_autogain_loudness_change:
                    result = await _drain_worker(
                        ee_manager.apply_autogain_loudness_runtime, previous, extras
                    )
                else:
                    result = await _drain_worker(
                        ee_manager.apply_global_extras_to_all_presets, extras
                    )
            except Exception:
                if disabling_master_percent is not None:
                    try:
                        await _drain_worker(set_output_volume, 100)
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
                await _load_easyeffects_preset(active_preset)
            except Exception as e:
                logger.warning("Failed to reload active preset after extras update: %s", e)
        if enabling_loudness:
            await _drain_worker(set_output_volume, 100)
    finally:
        if canonical_lock is not None:
            canonical_lock.release()

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
        async with _easyeffects_mutation_lock():
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
            await _load_easyeffects_preset(preset_name)
            compare = ee_manager.load_compare_state()
            if compare.get("presetA") == preset_name:
                compare["activeSide"] = "A"
                ee_manager.save_compare_state(compare)
            elif compare.get("presetB") == preset_name:
                compare["activeSide"] = "B"
                ee_manager.save_compare_state(compare)
            status = ee_manager.get_status()
        if (
            subwoofer_runtime is not None
            and subwoofer_runtime.snapshot().get("active")
            and not subwoofer_runtime.sync_in_progress
        ):
            # A running helper sync owns the graph and verifies/repairs its
            # own links; a concurrent reclean would race that repair.
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
            tmp_path = Path(tmp.name)
            await save_upload_to_file(file, tmp, EASYEEFFECTS_IR_MAX_BYTES)

        uploaded = await _run_locked_worker(
            _easyeffects_mutation_lock(),
            ee_manager.upload_ir,
            tmp_path,
            file.filename or tmp_path.name,
        )
        status = ee_manager.get_status()
        await manager.broadcast({"type": "easyeffects", "data": status})
        schedule_peak_monitor_refresh_after_effects_change("ir-upload")
        return {"status": "ok", "ir": uploaded}
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"EasyEffects IR upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Failed to remove uploaded IR temp file %s", tmp_path)

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

    try:
        # The canonical loudness read inside _effects_extras_from_form must
        # happen under the same mutation ownership as the preset creation:
        # a parallel extras mutation may never be frozen into the new preset
        # from a stale pre-lock snapshot.
        async with _easyeffects_mutation_lock():
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
        content = (await read_upload(file, EASYEEFFECTS_PRESET_TEXT_MAX_BYTES)).decode("utf-8-sig")
        async with _easyeffects_mutation_lock():
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
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Preset JSON is not valid UTF-8 text: {e}")
    except (ValueError, RuntimeError) as e:
        _raise_easyeffects_http_error(e)

# Preset bundle ZIP hardening limits (FXRoute bundles: manifest + preset
# JSON + a few IR files, each IR stored twice by name variant).
PRESET_BUNDLE_MAX_MEMBERS = 512
PRESET_BUNDLE_MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
PRESET_BUNDLE_MAX_MEMBER_BYTES = 256 * 1024 * 1024
PRESET_BUNDLE_MAX_JSON_BYTES = 16 * 1024 * 1024
PRESET_BUNDLE_MAX_CENTRAL_DIRECTORY_BYTES = 8 * 1024 * 1024

@app.post("/api/easyeffects/presets/import-bundle")
async def import_easyeffects_preset_bundle(
    file: UploadFile = File(...),
    load_after_create: bool = Form(False),
):
    ee_manager = _require_easyeffects_manager()

    temp_zip_path = None
    import_succeeded = False
    staged_irs = []
    ir_backups = []
    new_ir_destinations = []
    try:
        with tempfile.NamedTemporaryFile(prefix="fxroute-preset-import-", suffix=".zip", delete=False) as temp_file:
            temp_zip_path = Path(temp_file.name)
            await save_upload_to_file(file, temp_file, EASYEEFFECTS_BUNDLE_MAX_BYTES)

        zip_album.check_zip_file_before_open(
            temp_zip_path,
            max_members=PRESET_BUNDLE_MAX_MEMBERS,
            max_central_directory_bytes=PRESET_BUNDLE_MAX_CENTRAL_DIRECTORY_BYTES,
        )
        with zipfile.ZipFile(temp_zip_path) as archive:
            zip_album.check_zip_limits(
                archive,
                max_members=PRESET_BUNDLE_MAX_MEMBERS,
                max_total_uncompressed_bytes=PRESET_BUNDLE_MAX_TOTAL_UNCOMPRESSED_BYTES,
                max_member_bytes=PRESET_BUNDLE_MAX_MEMBER_BYTES,
            )
            safe_members = []
            for member in archive.infolist():
                if member.is_dir():
                    continue
                safe_relative = _is_safe_relative_zip_path(member.filename)
                if safe_relative is None:
                    raise zip_album.ZipLimitError(
                        f"Unsafe ZIP member path: {member.filename!r}"
                    )
                reason = zip_album.zip_member_hardening_reason(member)
                if reason:
                    raise zip_album.ZipLimitError(
                        f"Unsafe ZIP member {member.filename!r}: {reason}"
                    )
                safe_members.append((member, safe_relative))

            if archive.testzip() is not None:
                raise HTTPException(status_code=400, detail="Invalid ZIP archive")

            json_members = [(member, rel) for member, rel in safe_members if rel.suffix.lower() == ".json" and rel.name.lower() != "manifest.json"]
            preferred_json = next(((member, rel) for member, rel in json_members if rel.name.lower() == "preset.json"), None)
            if preferred_json is None:
                preferred_json = json_members[0] if len(json_members) == 1 else None
            if preferred_json is None:
                raise HTTPException(status_code=400, detail="Preset bundle must contain exactly one preset JSON")

            preset_member, preset_rel = preferred_json
            preset_text = zip_album.read_member_bounded(
                archive, preset_member, PRESET_BUNDLE_MAX_JSON_BYTES
            ).decode("utf-8-sig")
            try:
                preset_payload = json.loads(preset_text)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Preset JSON is invalid: {e}") from e
            kernel_names = ee_manager._extract_kernel_names_from_payload(preset_payload if isinstance(preset_payload, dict) else None)

            async with _easyeffects_mutation_lock():
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

                extracted_total = 0
                for _, (member, clean_ir_name) in sorted(ir_members_by_stem.items()):
                    destination = ee_manager.irs_dir / clean_ir_name
                    if destination.is_symlink():
                        raise ValueError(
                            f"Import blocked: existing IR path is a symlink: {clean_ir_name}"
                        )
                    if destination.is_dir():
                        raise ValueError(
                            f"Import blocked: existing IR path is a directory: {clean_ir_name}"
                        )
                    stage_path = ee_manager.irs_dir / f".fxroute-bundle-stage-{uuid4().hex}"
                    try:
                        remaining = min(
                            PRESET_BUNDLE_MAX_TOTAL_UNCOMPRESSED_BYTES - extracted_total,
                            PRESET_BUNDLE_MAX_MEMBER_BYTES,
                        )
                        written = zip_album.copy_member_bounded(
                            archive, member, stage_path, remaining_bytes=remaining
                        )
                    except BaseException:
                        stage_path.unlink(missing_ok=True)
                        raise
                    extracted_total += written
                    staged_irs.append(stage_path)
                    if destination.exists():
                        backup_path = ee_manager.irs_dir / f".fxroute-bundle-backup-{uuid4().hex}"
                        os.replace(destination, backup_path)
                        ir_backups.append((destination, backup_path))
                    else:
                        new_ir_destinations.append(destination)
                    os.replace(stage_path, destination)
                    imported_irs.append(destination.name)

                missing_kernels = [name for name in sorted(kernel_names) if not ee_manager._find_ir_paths_for_kernel_name(name)]
                if missing_kernels:
                    raise HTTPException(status_code=400, detail=f"Preset bundle is missing IR file(s): {', '.join(missing_kernels)}")

                preset_filename = preset_rel.name if preset_rel.name.lower() != "preset.json" else (Path(file.filename or "preset.json").stem + ".json")
                created = ee_manager.import_preset_json(preset_filename, preset_text)
            import_succeeded = True
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
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except zip_album.ZipLimitError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid ZIP archive")
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Preset JSON is not valid UTF-8 text: {e}")
    except (ValueError, RuntimeError) as e:
        _raise_easyeffects_http_error(e)
    finally:
        if import_succeeded:
            for _, backup_path in ir_backups:
                try:
                    backup_path.unlink(missing_ok=True)
                except Exception:
                    logger.exception("Failed to remove bundle IR backup %s", backup_path)
        else:
            for destination in new_ir_destinations:
                try:
                    destination.unlink(missing_ok=True)
                except Exception:
                    logger.exception("Failed to remove partially imported bundle IR %s", destination)
            for destination, backup_path in ir_backups:
                try:
                    os.replace(backup_path, destination)
                except Exception:
                    logger.exception("Failed to restore bundle IR backup %s", backup_path)
        for stage_path in staged_irs:
            try:
                stage_path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Failed to remove staged bundle IR %s", stage_path)
        if temp_zip_path is not None:
            try:
                temp_zip_path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Failed to remove imported preset bundle temp file %s", temp_zip_path)

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

    tmp_path = None
    try:
        suffix = Path(file.filename or "upload.ir").suffix
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = Path(tmp.name)
            await save_upload_to_file(file, tmp, EASYEEFFECTS_IR_MAX_BYTES)

        # The canonical loudness read inside _effects_extras_from_form must
        # happen under the same mutation ownership as the preset creation:
        # a parallel extras mutation during the (potentially slow) upload
        # staging may never be frozen into the new preset from a stale
        # pre-lock snapshot.  The lock is acquired here (not via
        # _run_locked_worker, which would re-acquire) and the blocking
        # manager call runs through the cancellation-safe worker.
        async with _easyeffects_mutation_lock():
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
            created = await _drain_worker(
                ee_manager.create_convolver_preset_with_upload,
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
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        _raise_easyeffects_http_error(e)
    except Exception as e:
        logger.error(f"EasyEffects create-with-ir failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Failed to remove create-with-ir temp file %s", tmp_path)

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
        async with _easyeffects_mutation_lock():
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
        content = await read_upload(file, EASYEEFFECTS_PRESET_TEXT_MAX_BYTES)
        rew_text = content.decode("utf-8-sig")
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="REW import file must be UTF-8 text")

    if not preset_name.strip():
        raise HTTPException(status_code=400, detail="preset_name is required")

    try:
        # Canonical extras resolution under the same mutation ownership as
        # the import: never freeze a stale pre-lock loudness snapshot into
        # the new preset.
        async with _easyeffects_mutation_lock():
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

    # Shared form values; the canonical loudness read itself happens only
    # inside the mutation ownership below, per import branch.
    def _form_extras_kwargs() -> dict:
        return dict(
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
                    tmp_path = Path(tmp.name)
                    # Register before writing so a read/write failure still
                    # cleans up the partial file in the outer finally.
                    tmp_paths.append(tmp_path)
                    await save_upload_to_file(upload, tmp, EASYEEFFECTS_IR_MAX_BYTES)
                    return tmp_path

            left_tmp = await _save_temp(left_file)
            right_tmp = await _save_temp(right_file)

            # Canonical extras resolution under the same mutation ownership
            # as the creation; the blocking manager call runs through the
            # cancellation-safe worker (the lock is acquired here, not via
            # _run_locked_worker).
            async with _easyeffects_mutation_lock():
                extras = _effects_extras_from_form(**_form_extras_kwargs())
                created = await _drain_worker(
                    ee_manager.create_convolver_preset_with_dual_uploads,
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
                    left_text = (await read_upload(left_file, EASYEEFFECTS_PRESET_TEXT_MAX_BYTES)).decode("utf-8-sig")
                    right_text = (await read_upload(right_file, EASYEEFFECTS_PRESET_TEXT_MAX_BYTES)).decode("utf-8-sig")
                except UnicodeDecodeError:
                    raise HTTPException(status_code=400, detail="Dual REW import files must be UTF-8 text")

            left_text = str(left_text or "").strip()
            right_text = str(right_text or "").strip()
            if not left_text or not right_text:
                raise HTTPException(status_code=400, detail="Provide Left and Right REW text, or Left and Right .irs/.wav files")

            async with _easyeffects_mutation_lock():
                extras = _effects_extras_from_form(**_form_extras_kwargs())
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
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except (ValueError, RuntimeError) as e:
        _raise_easyeffects_http_error(e)
    finally:
        for tmp_path in tmp_paths:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Failed to remove dual import temp file %s", tmp_path)

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
        async with _easyeffects_mutation_lock():
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
            _create_library_refresh_task(
                library_scanner,
                name="manual-library-refresh",
            )
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
    global current_footer_owner, latest_spotify_state
    # Nach dem Coordinator-Commit ist die Spotify-Source bereits der committed
    # Playback-Kontext; der anschliessende State-Read ist Telemetry/UI-Refresh
    # und kein Teil der Ownership-Boundary.  Publiziere synchron vor jedem
    # weiteren await, damit der Ended-Waiter nie ein Fenster mit neuem Token
    # und altem Footer sieht.
    current_footer_owner = "spotify"
    _publish_playback_context_commit(getattr(result, "transition_id", None))
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
    global current_footer_owner
    # Derselbe Ownership-Vertrag wie api_spotify_play: Footer und Token
    # werden synchron nach dem Commit publiziert, bevor der Spotify-State
    # gelesen oder gebroadcastet wird.
    current_footer_owner = "spotify"
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
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            if text is not None:
                await manager.send_to_client(websocket, json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"WebSocket error: {e}")
    finally:
        await manager.disconnect(websocket)

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
