# SPDX-License-Identifier: AGPL-3.0-only

"""Main FastAPI application for FXRoute."""

import json
import logging
import re
import shutil
import time
import asyncio
import hashlib
import random
import subprocess
import tempfile
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import quote, unquote
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from mutagen import File as MutagenFile
from starlette.background import BackgroundTask

from config import get_settings

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
COVER_CACHE_DIR = BASE_DIR / "media" / "cache" / "covers"
TOP40_COVER_IMAGE = STATIC_DIR / "Top40.png"
INSTALL_CONFIG_FILE = Path.home() / ".config" / "fxroute" / "install-config.env"
UPDATE_SCRIPT = BASE_DIR / "scripts" / "update_fxroute.sh"

# Cooldown to prevent rapid mpv IPC flooding (ms)
PLAY_COMMAND_COOLDOWN_MS = 400
LOCAL_TRACK_SWITCH_SETTLE_MS = 260
SOURCE_HANDOFF_SETTLE_MS = 260
PIPEWIRE_HANDOFF_RELEASE_TIMEOUT_MS = 1800
PIPEWIRE_HANDOFF_POLL_INTERVAL_MS = 50
PEAK_MONITOR_INACTIVE_GRACE_MS = 450
PEAK_MONITOR_RESTART_SETTLE_MS = 320
PEAK_MONITOR_RATE_MATCH_TIMEOUT_MS = 900
RADIO_SAMPLERATE_RENEGOTIATE_DELAY_MS = 1200
RADIO_SAMPLERATE_PRESET_BOUNCE_DELAY_MS = 350
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
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
    except Exception:
        return False
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


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
    try:
        return (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _read_build_id() -> str:
    version = _read_version_file() or "unknown-version"
    try:
        deployed_build = (BASE_DIR / "BUILD_ID").read_text(encoding="utf-8").strip()
        if deployed_build:
            return f"{version} {deployed_build}"
    except Exception:
        pass
    try:
        completed = subprocess.run(
            ["git", "-C", str(BASE_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.5,
        )
        commit = completed.stdout.strip() if completed.returncode == 0 else ""
    except Exception:
        commit = ""
    return f"{version} commit={commit or 'unknown'}"


def _read_install_config() -> dict:
    data: dict[str, str] = {}
    try:
        for raw_line in INSTALL_CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
    except Exception:
        pass
    return data


def _configured_service_name() -> str:
    return _read_install_config().get("FXROUTE_SERVICE_NAME") or "fxroute"


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
    try:
        completed = subprocess.run(["pactl", "list", "sink-inputs"], capture_output=True, text=True, check=False, timeout=1.5)
    except Exception:
        return []
    if completed.returncode != 0:
        return []

    entries: list[dict] = []
    current: dict | None = None
    in_properties = False
    for raw_line in completed.stdout.splitlines():
        if raw_line.startswith("Sink Input #"):
            if current:
                entries.append(current)
            current = {"id": raw_line.split("#", 1)[1].strip(), "properties": {}}
            in_properties = False
            continue
        if current is None:
            continue

        stripped = raw_line.strip()
        if stripped.startswith("Sample Specification:"):
            match = re.search(r"(\d+)\s*Hz\b", stripped)
            if match:
                try:
                    current["sample_rate"] = int(match.group(1))
                except ValueError:
                    pass
            continue
        if stripped.startswith("Sink:"):
            current["sink"] = stripped.split(":", 1)[1].strip()
            continue
        if stripped.startswith("Corked:"):
            current["corked"] = stripped.split(":", 1)[1].strip().lower() == "yes"
            continue
        if stripped.startswith("Mute:"):
            current["muted"] = stripped.split(":", 1)[1].strip().lower() == "yes"
            continue
        if stripped.startswith("Volume:"):
            match = re.search(r"/\s*(\d+)%\s*/", stripped)
            if match:
                try:
                    current["volume_percent"] = int(match.group(1))
                except ValueError:
                    pass
            continue
        if stripped == "Properties:":
            in_properties = True
            continue
        if not stripped:
            in_properties = False
            continue
        if in_properties:
            if " = " not in stripped:
                continue
            key, value = stripped.split(" = ", 1)
            current["properties"][key.strip()] = value.strip().strip('"')

    if current:
        entries.append(current)
    return entries


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


def _get_first_sink_input_samplerate(entries: list[dict]) -> Optional[int]:
    for entry in entries:
        rate = entry.get("sample_rate")
        if isinstance(rate, int) and rate > 0:
            return rate
    return None


async def _wait_for_sink_input_release(list_fn, timeout_ms: int) -> bool:
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while time.monotonic() <= deadline:
        if not list_fn():
            return True
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
    return not list_fn()


async def _wait_for_pipewire_mpv_release(timeout_ms: int = PIPEWIRE_HANDOFF_RELEASE_TIMEOUT_MS) -> bool:
    return await _wait_for_sink_input_release(_list_mpv_sink_inputs, timeout_ms)


async def _wait_for_pipewire_spotify_release(timeout_ms: int = PIPEWIRE_HANDOFF_RELEASE_TIMEOUT_MS) -> bool:
    return await _wait_for_sink_input_release(_list_spotify_sink_inputs, timeout_ms)


async def _wait_for_pipewire_spotify_samplerate_alignment(
    timeout_ms: int = PIPEWIRE_HANDOFF_RELEASE_TIMEOUT_MS,
) -> tuple[bool, Optional[int], Optional[int]]:
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    last_stream_rate: Optional[int] = None
    last_sink_rate: Optional[int] = None
    while time.monotonic() <= deadline:
        spotify_inputs = _list_spotify_sink_inputs()
        last_stream_rate = _get_first_sink_input_samplerate(spotify_inputs)
        if spotify_inputs and last_stream_rate:
            try:
                samplerate_status = get_samplerate_status()
            except Exception:
                samplerate_status = {}
            sink_rate = samplerate_status.get("active_rate")
            last_sink_rate = sink_rate if isinstance(sink_rate, int) and sink_rate > 0 else None
            if last_sink_rate == last_stream_rate:
                return True, last_stream_rate, last_sink_rate
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
    return False, last_stream_rate, last_sink_rate


async def _recover_spotify_samplerate_alignment() -> tuple[bool, Optional[int], Optional[int]]:
    """Retry a stuck Spotify start with one controlled pause/play release cycle."""
    global source_transition_lock
    if source_transition_lock is None:
        source_transition_lock = asyncio.Lock()
    async with source_transition_lock:
        data = await spotify_pause()
        released = await _wait_for_pipewire_spotify_release()
        if not released:
            await asyncio.sleep(SOURCE_HANDOFF_SETTLE_MS / 1000)
        await _prearm_spotify_samplerate("spotify-recovery")
        data = await spotify_play()
        if data.get("status") != "Playing":
            return False, None, None
        return await _wait_for_pipewire_spotify_samplerate_alignment()


async def _complete_spotify_entry_handoff() -> dict:
    global spotify_samplerate_recovery_active
    await pause_local_playback_for_spotify_broadcast()
    await _prearm_spotify_samplerate("spotify-entry-handoff")
    await asyncio.sleep(SOURCE_HANDOFF_SETTLE_MS / 1000)
    data = await spotify_play()
    if data.get("status") == "Playing":
        aligned, stream_rate, sink_rate = await _wait_for_pipewire_spotify_samplerate_alignment()
        if not aligned and not spotify_samplerate_recovery_active:
            logger.warning(
                "Spotify samplerate did not align on entry handoff; leaving recovery to watcher: spotify_stream_rate=%s sink_rate=%s",
                stream_rate,
                sink_rate,
            )
        elif not aligned:
            logger.info(
                "Spotify samplerate still settling during active recovery: spotify_stream_rate=%s sink_rate=%s",
                stream_rate,
                sink_rate,
            )
    return data


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


async def _release_local_samplerate_prearm(expected_rate: int, generation: int, reason: str) -> None:
    global local_samplerate_prearm_generation, radio_samplerate_force_rate
    try:
        aligned = await _wait_for_samplerate_alignment(expected_rate, timeout_ms=1200)
        if generation != local_samplerate_prearm_generation:
            return
        if current_track_info and current_track_info.get("source") in {"local", "radio"}:
            radio_samplerate_force_rate = expected_rate
            logger.info(
                "Local samplerate pre-arm retained for active playback: reason=%s expected_rate=%s aligned=%s source=%s",
                reason,
                expected_rate,
                aligned,
                current_track_info.get("source"),
            )
            return
        _set_pipewire_force_rate(0)
        logger.info(
            "Local samplerate pre-arm released: reason=%s expected_rate=%s aligned=%s",
            reason,
            expected_rate,
            aligned,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Local samplerate pre-arm release failed: reason=%s expected_rate=%s error=%s",
            reason,
            expected_rate,
            exc,
        )


async def _prearm_known_local_samplerate(track_info: dict | None, reason: str) -> tuple[Optional[int], Optional[int]]:
    global local_samplerate_prearm_generation, radio_samplerate_force_rate
    track_info = track_info or {}
    if track_info.get("source") != "local":
        return None, None

    target_rate = track_info.get("sample_rate_hz")
    if not isinstance(target_rate, int) or target_rate <= 0:
        logger.info("Local samplerate pre-arm skipped: no known sample_rate_hz reason=%s", reason)
        return None, None

    try:
        samplerate_status = get_samplerate_status()
    except Exception as exc:
        logger.info("Local samplerate pre-arm skipped: samplerate status unavailable reason=%s error=%s", reason, exc)
        return None, None

    active_rate = samplerate_status.get("active_rate")
    force_rate = samplerate_status.get("force_rate")
    allowed_rates = samplerate_status.get("allowed_rates") or []
    if allowed_rates and target_rate not in allowed_rates:
        logger.info(
            "Local samplerate pre-arm skipped: target_rate=%s not in allowed_rates=%s reason=%s",
            target_rate,
            allowed_rates,
            reason,
        )
        return None, None

    if active_rate == target_rate and force_rate in {None, 0, target_rate}:
        logger.info(
            "Local samplerate pre-arm not needed: reason=%s target_rate=%s active_rate=%s force_rate=%s",
            reason,
            target_rate,
            active_rate,
            force_rate,
        )
        return None, None

    _set_pipewire_force_rate(target_rate)
    radio_samplerate_force_rate = None
    local_samplerate_prearm_generation += 1
    generation = local_samplerate_prearm_generation
    logger.info(
        "Local samplerate pre-arm applied: reason=%s target_rate=%s active_rate=%s force_rate=%s title=%s",
        reason,
        target_rate,
        active_rate,
        force_rate,
        track_info.get("title") or track_info.get("id"),
    )
    return target_rate, generation


def _get_current_pipewire_force_rate() -> Optional[int]:
    try:
        status = get_samplerate_status()
    except Exception:
        return None
    force_rate = status.get("force_rate") if isinstance(status, dict) else None
    return force_rate if isinstance(force_rate, int) and force_rate > 0 else 0


async def _ensure_radio_samplerate_force(expected_rate: Optional[int], reason: str) -> bool:
    global radio_samplerate_force_rate
    if not isinstance(expected_rate, int) or expected_rate <= 0:
        return False
    try:
        samplerate_status = get_samplerate_status()
    except Exception:
        samplerate_status = {}
    active_rate = samplerate_status.get("active_rate") if isinstance(samplerate_status, dict) else None
    force_rate = samplerate_status.get("force_rate") if isinstance(samplerate_status, dict) else None
    if active_rate == expected_rate and force_rate == expected_rate:
        radio_samplerate_force_rate = expected_rate
        return True
    if force_rate != expected_rate:
        _set_pipewire_force_rate(expected_rate)
        radio_samplerate_force_rate = expected_rate
        logger.info(
            "Radio samplerate force-rate applied: reason=%s expected_rate=%s active_rate=%s previous_force_rate=%s",
            reason,
            expected_rate,
            active_rate,
            force_rate,
        )
    aligned = await _wait_for_samplerate_alignment(expected_rate, timeout_ms=400)
    if not aligned and isinstance(active_rate, int) and active_rate != expected_rate:
        if reason not in {"radio-start-before-loadfile", "radio-restart-after-measurement"}:
            logger.info(
                "Radio samplerate sink suspend/resume SKIPPED: reason=%s (only for radio-start/restart paths)",
                reason,
            )
        else:
            suspended = await _suspend_resume_playback_sink(reason=reason, force=True)
            if suspended:
                aligned = await _wait_for_samplerate_alignment(expected_rate, timeout_ms=1200)
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
    return aligned


def _clear_radio_samplerate_force_if_active(reason: str) -> None:
    global radio_samplerate_force_rate
    if not radio_samplerate_force_rate:
        return
    current_force_rate = _get_current_pipewire_force_rate()
    if current_force_rate == radio_samplerate_force_rate:
        try:
            _set_pipewire_force_rate(0)
            logger.info("Radio samplerate force-rate released: reason=%s previous_force_rate=%s", reason, radio_samplerate_force_rate)
        except Exception as exc:
            logger.warning("Radio samplerate force-rate release failed: reason=%s error=%s", reason, exc)
            return
    radio_samplerate_force_rate = None


async def _prearm_spotify_samplerate(reason: str) -> None:
    global radio_samplerate_force_rate
    expected_rate = SPOTIFY_PREARM_SAMPLE_RATE_HZ
    try:
        samplerate_status = get_samplerate_status()
    except Exception:
        samplerate_status = {}
    active_rate = samplerate_status.get("active_rate") if isinstance(samplerate_status, dict) else None
    force_rate = samplerate_status.get("force_rate") if isinstance(samplerate_status, dict) else None
    if force_rate != expected_rate:
        _set_pipewire_force_rate(expected_rate)
        logger.info(
            "Spotify samplerate pre-arm applied: reason=%s expected_rate=%s active_rate=%s previous_force_rate=%s",
            reason,
            expected_rate,
            active_rate,
            force_rate,
        )
    radio_samplerate_force_rate = expected_rate
    await _wait_for_samplerate_alignment(expected_rate, timeout_ms=700)


def _is_local_playback_active(state: dict | None) -> bool:
    state = state or {}
    return bool(state.get("current_file") and not state.get("paused") and not state.get("ended"))


def _is_spotify_playback_active(state: dict | None) -> bool:
    state = state or {}
    return bool(state.get("available") and state.get("status") == "Playing")


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
    if _has_local_footer_context(playback_state):
        current_footer_owner = "local"
        return current_footer_owner
    if _is_spotify_playback_active(spotify_state):
        current_footer_owner = "spotify"
        return current_footer_owner
    return current_footer_owner or "local"


async def _apply_hard_playback_handoff(previous_file: Optional[str], next_url: Optional[str], handoff_reason: Optional[str], transition_reason: str) -> None:
    if not player_instance:
        return
    logger.info(
        "Applying hard handoff before %s (%s): %s -> %s",
        transition_reason,
        handoff_reason,
        previous_file,
        next_url,
    )
    player_instance.stop_playback()
    released = await _wait_for_pipewire_mpv_release()
    if not released:
        settle_ms = LOCAL_TRACK_SWITCH_SETTLE_MS if handoff_reason == "manual local track switch" else SOURCE_HANDOFF_SETTLE_MS
        logger.warning(
            "Timed out waiting for mpv PipeWire stream release before %s; falling back to %sms settle",
            transition_reason,
            settle_ms,
        )
        await asyncio.sleep(settle_ms / 1000)


def _dedupe_archive_name(name: str, used_names: set[str]) -> str:
    candidate = Path(name or "track").name or "track"
    stem = Path(candidate).stem or "track"
    suffix = Path(candidate).suffix
    index = 2
    while candidate in used_names:
        candidate = f"{stem}-{index}{suffix}"
        index += 1
    used_names.add(candidate)
    return candidate

from models import (
    DeleteFolderRequest,
    DeleteTracksRequest,
    DownloadTracksRequest,
    PlaylistSaveRequest,
    PlayRequest,
    StationUpsertRequest,
)
from pydantic import BaseModel
from player import get_player, MPVNotInstalledError, MPVError
from stations import add_station, delete_station, get_stations, update_station
from playlists import delete_playlist, get_playlists, save_playlist
from library import AUDIO_EXTENSIONS, LibraryScanner
from downloader import Downloader
from easyeffects import EasyEffectsManager
try:
    from hardware_controller import HardwareController
except ImportError:
    HardwareController = None
from measurement import MeasurementStore, score_sub_alignment_candidates
from peak_monitor import EasyEffectsPeakMonitor
from subwoofer_runtime import Subwoofer21Runtime, SubwooferRuntimeConfig, DEFAULT_SAMPLE_RATE


REMOVABLE_ARTWORK_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
REMOVABLE_ARTWORK_STEMS = {"cover", "folder", "front", "albumart"}
REMOVABLE_EMPTY_SIDECAR_SUFFIXES = {".m3u", ".m3u8", ".cue", ".log", ".nfo", ".txt"}


def _is_removable_artwork_file(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() not in REMOVABLE_ARTWORK_SUFFIXES:
        return False
    name = path.name.lower()
    stem = path.stem.lower()
    folder_stem = path.parent.name.lower()
    return stem in REMOVABLE_ARTWORK_STEMS or stem.startswith("albumart") or any(
        token in name for token in ("cover", "folder", "front", "album", "artwork")
    ) or stem == folder_stem


def _is_removable_metadata_sidecar(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() not in REMOVABLE_EMPTY_SIDECAR_SUFFIXES:
        return False
    return True


def _is_cleanup_only_file(path: Path) -> bool:
    if not path.is_file():
        return False
    return path.suffix.lower() in REMOVABLE_ARTWORK_SUFFIXES or _is_removable_metadata_sidecar(path)


def _folder_has_audio_files(folder: Path) -> bool:
    try:
        for child in folder.iterdir():
            if child.is_file() and child.suffix.lower() in AUDIO_EXTENSIONS:
                return True
    except OSError:
        return False
    return False


def _cleanup_track_parent_folder(folder: Path, music_root: Path, protected_folders: Optional[set[Path]] = None) -> dict:
    cleaned = {"folder": str(folder), "removed_files": [], "removed_folder": False, "kept": []}
    protected = {item.resolve() for item in (protected_folders or set())}
    if (
        not folder.is_dir()
        or not _path_within_root(folder, music_root)
        or folder.resolve() == music_root.resolve()
        or folder.resolve() in protected
    ):
        return cleaned
    if _folder_has_audio_files(folder):
        return cleaned

    try:
        children = list(folder.iterdir())
    except OSError as exc:
        cleaned["kept"].append({"path": str(folder), "reason": str(exc)})
        return cleaned

    files = [child for child in children if child.is_file()]
    cleanup_only_folder = bool(files) and all(_is_cleanup_only_file(child) for child in files)

    for child in children:
        if cleanup_only_folder or _is_removable_artwork_file(child) or _is_removable_metadata_sidecar(child):
            try:
                child.unlink()
                cleaned["removed_files"].append(str(child))
            except OSError as exc:
                cleaned["kept"].append({"path": str(child), "reason": str(exc)})

    try:
        if not any(folder.iterdir()):
            folder.rmdir()
            cleaned["removed_folder"] = True
    except OSError as exc:
        cleaned["kept"].append({"path": str(folder), "reason": str(exc)})
    return cleaned


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
    set_audio_output_mode,
    set_audio_output_selection,
    set_audio_source_selection,
    set_bluetooth_receiver_enabled,
)
from spotify import (
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

UPLOAD_AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav", ".wma", ".webm", ".weba"}
PLAYLIST_FILE_EXTENSIONS = {".m3u", ".m3u8"}
ZIP_IGNORED_PARTS = {"__MACOSX"}
ZIP_IGNORED_FILENAMES = {".ds_store", "thumbs.db"}

# Global instances (initialized on startup)
settings = None
player_instance = None
library_scanner = None
downloader = None
easyeffects_manager = None
measurement_store = None
peak_monitor = None
subwoofer_runtime = None
subwoofer_runtime_link_watch_task = None
hardware_controller = None
peak_monitor_playback_armed = False
peak_monitor_transition_lock = None
peak_monitor_context_signature = None
easyeffects_preset_load_lock = None
source_transition_lock = None
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
spotify_samplerate_recovery_lock = None
spotify_samplerate_recovery_active = False
local_samplerate_prearm_generation = 0
radio_samplerate_force_rate = None
current_source_mode = SOURCE_MODE_APP_PLAYBACK
latest_spotify_state = None
current_footer_owner = "local"
last_measurement_window_seen_at = 0.0
last_spotify_samplerate_recovery_at = 0.0
last_app_samplerate_drift_repair_at = 0.0
silent_active_recovery_attempts: set[str] = set()
silent_active_watch_tasks: dict[str, asyncio.Task] = {}
latest_player_state_seq_seen = 0
current_track_info = None
last_track_info = None
last_radio_track_info = None
radio_reconnect_task = None
radio_reconnect_attempts = 0
radio_reconnect_url = None
radio_reconnect_active_since = 0.0
radio_stream_stale_after_measurement = False
_radio_state_before_measurement: dict[str, Any] | None = None
playback_stream_stale_after_measurement = False
_playback_state_before_measurement: dict[str, Any] | None = None
playback_queue = []
playback_queue_original = []
playback_queue_index = -1
playback_queue_mode = "app_replace"
queue_advancing = False
queue_transition_target_url = None
playback_queue_loop = False
playback_queue_shuffle = False
single_track_loop = False

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
    if not path.exists():
        return path

    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _choose_unique_dir(path: Path) -> Path:
    if not path.exists():
        return path

    counter = 2
    while True:
        candidate = path.with_name(f"{path.name}-{counter}")
        if not candidate.exists():
            return candidate
        counter += 1


def _is_safe_relative_zip_path(name: str) -> Optional[Path]:
    normalized = name.replace("\\", "/").strip("/")
    if not normalized:
        return None

    candidate = Path(normalized)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    if any(part in ZIP_IGNORED_PARTS for part in candidate.parts):
        return None
    if candidate.name.lower() in ZIP_IGNORED_FILENAMES:
        return None
    return candidate


def _extract_zip_album(zip_path: Path, target_root: Path) -> dict:
    extracted_files = []
    skipped_entries = []

    try:
        with zipfile.ZipFile(zip_path) as archive:
            if archive.testzip() is not None:
                raise HTTPException(status_code=400, detail="Invalid ZIP archive")

            for member in archive.infolist():
                safe_relative = _is_safe_relative_zip_path(member.filename)
                if safe_relative is None:
                    skipped_entries.append(member.filename)
                    continue

                if member.is_dir():
                    (target_root / safe_relative).mkdir(parents=True, exist_ok=True)
                    continue

                destination = target_root / safe_relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.suffix.lower() in UPLOAD_AUDIO_EXTENSIONS:
                    destination = _choose_unique_path(destination)

                with archive.open(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)

                extracted_files.append(destination)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid ZIP archive")

    audio_files = [path for path in extracted_files if path.suffix.lower() in UPLOAD_AUDIO_EXTENSIONS]
    playlist_files = [path for path in extracted_files if path.suffix.lower() in PLAYLIST_FILE_EXTENSIONS]
    return {
        "audio_files": audio_files,
        "playlist_files": playlist_files,
        "extracted_files": extracted_files,
        "skipped_entries": skipped_entries,
    }


def _parse_m3u_entries(content: str) -> List[str]:
    entries = []
    for raw_line in (content or "").splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    return entries


def _playlist_download_filename(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "playlist").strip()).strip("-._")
    return f"{slug or 'playlist'}.m3u8"


def _track_relative_m3u_path(track) -> str:
    if track.path and settings:
        try:
            return track.path.resolve().relative_to(settings.MUSIC_ROOT.resolve()).as_posix()
        except Exception:
            pass
    return Path(track.url or track.id).name


def _build_m3u_for_playlist(playlist) -> str:
    tracks_by_id = {track.id: track for track in library_scanner.get_tracks(refresh=True)}
    lines = ["#EXTM3U"]
    for track_id in playlist.track_ids:
        track = tracks_by_id.get(track_id)
        if not track:
            continue
        duration = int(track.duration) if track.duration and track.duration > 0 else -1
        label = track.title or Path(track.path or track_id).stem
        if track.artist:
            label = f"{track.artist} - {label}"
        lines.append(f"#EXTINF:{duration},{label}")
        lines.append(_track_relative_m3u_path(track))
    return "\n".join(lines) + "\n"


def _build_track_match_index(tracks) -> dict[str, str]:
    matches = {}
    ambiguous = set()

    def add(key: str, track_id: str) -> None:
        key = (key or "").replace("\\", "/").strip().lstrip("./").lower()
        if not key:
            return
        if key in matches and matches[key] != track_id:
            ambiguous.add(key)
            matches.pop(key, None)
            return
        if key not in ambiguous:
            matches[key] = track_id

    for track in tracks:
        if not track.path:
            continue
        path = track.path.resolve()
        try:
            rel = path.relative_to(settings.MUSIC_ROOT.resolve()).as_posix()
            add(rel, track.id)
        except Exception:
            pass
        add(path.as_posix(), track.id)
        add(path.name, track.id)
        if track.url:
            add(str(track.url), track.id)
    return matches


def _resolve_m3u_track_ids(entries: List[str], base_dir: Optional[Path] = None, tracks=None) -> List[str]:
    if tracks is None:
        tracks = library_scanner.get_tracks(refresh=True)
    match_index = _build_track_match_index(tracks)
    track_ids = []
    seen = set()

    for entry in entries:
        value = unquote(entry.strip().strip('"'))
        if value.lower().startswith("file://"):
            value = value[7:]
        value = value.replace("\\", "/")
        candidates = [value]
        if base_dir and not Path(value).is_absolute():
            try:
                resolved = (base_dir / value).resolve()
                candidates.append(resolved.as_posix())
                candidates.append(resolved.relative_to(settings.MUSIC_ROOT.resolve()).as_posix())
            except Exception:
                pass
        candidates.append(Path(value).name)

        for candidate in candidates:
            track_id = match_index.get(candidate.replace("\\", "/").strip().lstrip("./").lower())
            if track_id and track_id not in seen:
                seen.add(track_id)
                track_ids.append(track_id)
                break
    return track_ids


def _import_m3u_playlist(name: str, content: str, base_dir: Optional[Path] = None, tracks=None) -> Optional[dict]:
    entries = _parse_m3u_entries(content)
    track_ids = _resolve_m3u_track_ids(entries, base_dir=base_dir, tracks=tracks)
    if not track_ids:
        return None
    playlist = save_playlist(Path(name).stem or "Imported playlist", track_ids)
    return {
        "id": playlist.id,
        "name": playlist.name,
        "track_ids": playlist.track_ids,
        "track_count": len(playlist.track_ids),
        "matched_track_count": len(track_ids),
        "entry_count": len(entries),
    }


def _clear_playback_queue():
    global playback_queue, playback_queue_original, playback_queue_index, playback_queue_mode, queue_transition_target_url, playback_queue_loop, playback_queue_shuffle, single_track_loop
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


def _should_use_mpv_native_queue(ordered_tracks: list[dict]) -> bool:
    if len(ordered_tracks) <= 1:
        return False

    sample_rates = set()
    for track in ordered_tracks:
        if track.get("source") != "local":
            return False
        if not track.get("url"):
            return False
        sample_rate_hz = track.get("sample_rate_hz")
        if not isinstance(sample_rate_hz, int) or sample_rate_hz <= 0:
            return False
        sample_rates.add(sample_rate_hz)

    return len(sample_rates) == 1


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
    player_instance.set_loop_playlist(False)
    player_instance.set_loop_file(False)


def _prime_mpv_native_queue(start_index: int) -> bool:
    if len(playback_queue) <= 1:
        return False

    first_url = playback_queue[0].get("url")
    if not first_url:
        return False

    player_instance.set_pause(True)
    player_instance.loadfile(first_url, mode="replace")
    for item in playback_queue[1:]:
        item_url = item.get("url")
        if not item_url:
            return False
        player_instance.loadfile(item_url, mode="append")
    if start_index > 0:
        player_instance.set_playlist_pos(start_index)
    player_instance.set_loop_playlist(playback_queue_loop)
    player_instance.set_loop_file(False)
    player_instance.set_pause(False)
    return True


def _trim_mpv_native_queue_to_current() -> None:
    if not player_instance or not player_instance._running:
        return
    current_index = playback_queue_index
    playlist_count = player_instance.get_property("playlist-count")
    if not isinstance(current_index, int) or current_index < 0:
        return
    if not isinstance(playlist_count, int) or playlist_count <= 1:
        player_instance.set_loop_playlist(False)
        return
    for index in range(playlist_count - 1, -1, -1):
        if index == current_index:
            continue
        player_instance.remove_playlist_index(index)
    player_instance.set_loop_playlist(False)


def _should_apply_hard_handoff_for_requested_play(*, requested_source: str, previous_source: Optional[str], previous_file: Optional[str], next_url: Optional[str]) -> tuple[bool, Optional[str]]:
    if not previous_file or not next_url or previous_file == next_url:
        return False, None

    if requested_source == "local" and previous_source == "local":
        return True, "manual local track switch"

    if requested_source in {"local", "radio"} and previous_source in {"local", "radio"} and requested_source != previous_source:
        return True, f"source change {previous_source}->{requested_source}"

    return False, None


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
    state = state or {}
    track = track or {}
    source = track.get("source")
    current_file = state.get("current_file")
    track_url = track.get("url")
    if source in {"local", "radio"} and current_file and track_url and current_file != track_url:
        return False
    return True


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
    result = []
    for entry in entries:
        props = entry.get("properties") or {}
        result.append({
            "id": entry.get("id"),
            "sink": entry.get("sink"),
            "node": props.get("node.name"),
            "app": props.get("application.name") or props.get("application.id"),
            "media": props.get("media.name"),
            "rate": entry.get("sample_rate"),
            "corked": entry.get("corked"),
            "muted": entry.get("muted"),
            "volume_percent": entry.get("volume_percent"),
        })
    return result


def _active_unmuted_sink_inputs(entries: list[dict]) -> list[dict]:
    return [
        entry for entry in entries
        if not entry.get("corked")
        and not entry.get("muted")
        and int(entry.get("volume_percent") or 100) > 0
    ]


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


async def _confirm_configured_default_sink(output_mode: dict) -> bool:
    output_key = str(output_mode.get("effective_output_key") or "").strip()
    if not output_key or output_key == "easyeffects_sink":
        return False
    try:
        await _run_pactl_command("set-default-sink", output_key)
        logger.info("Silent-active recovery confirmed default sink: %s", output_key)
        return True
    except Exception as exc:
        logger.warning("Silent-active recovery default sink confirm failed: output=%s error=%s", output_key, exc)
        return False


async def _resync_output_graph_for_current_mode(overview: dict) -> None:
    output_mode = overview.get("output_mode") or {}
    mode = output_mode.get("mode") or OUTPUT_MODE_STEREO
    if mode == OUTPUT_MODE_STEREO:
        await _ensure_stereo_easyeffects_output_graph(overview)
        return
    if subwoofer_runtime is not None:
        await _sync_subwoofer_runtime(overview)


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

    # PATCH silent-active-neutralize (2026-07-07):
    # Skip detection when the EE output peak meter has not yet observed a
    # sample. During EE preset / convolver reload, output_peak.detected
    # stays False and vu_db falls back to its default (-60). Treating that
    # as "silent audio" caused a loadfile() recovery loop on every library
    # start. Testing confirmed real audio on UMC even with the EE Output
    # Meter reporting -60.
    output_peak = peak_snapshot.get("output_peak") or {}
    if not output_peak.get("detected"):
        logger.info(
            "SILENT-ACTIVE-DIAG skip: peak_not_detected vu_db=%s source=%s signature=%s",
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


async def _recover_silent_active_mpv_source(track: dict | None) -> None:
    if not player_instance or not player_instance._running or not track:
        return
    if not _current_track_matches(track):
        return
    url = track.get("url")
    if not url:
        return
    logger.warning("Silent-active recovery reloading mpv source once: source=%s url=%s", track.get("source"), url)
    player_instance.loadfile(url, mode="replace")
    player_instance.set_pause(False)
    _mark_player_state_authoritative(player_instance.state)
    asyncio.create_task(_sync_peak_monitor_after_playback_transition(track.copy()))
    asyncio.create_task(_maybe_recover_samplerate_mismatch(track.copy()))
    if subwoofer_runtime is not None:
        asyncio.create_task(_sync_subwoofer_runtime_after_playback_transition(track.copy()))


async def _recover_silent_active_spotify(signature: str) -> None:
    global latest_spotify_state
    logger.warning("Silent-active recovery re-running Spotify handoff once: signature=%s", signature)
    data = await _complete_spotify_entry_handoff()
    latest_spotify_state = {**(latest_spotify_state or {}), **data}
    await sync_peak_monitor_for_spotify_state(data)


async def _sync_peak_monitor_after_playback_transition(expected_track: dict | None, timeout_ms: int = 2500) -> None:
    if not expected_track or expected_track.get("source") not in {"local", "radio"}:
        return
    settled = await _wait_for_player_current_file(expected_track.get("url"), timeout_ms=timeout_ms)
    if not settled:
        logger.info(
            "Player transition still settling after play command: source=%s requested_url=%s state_file=%s",
            expected_track.get("source"),
            expected_track.get("url"),
            (player_instance.state if player_instance else {}).get("current_file"),
        )
        return
    if _current_track_matches(expected_track):
        # Skip redundant peak-monitor restart when on_player_state_change
        # has already armed it. A duplicate restart ~2.5 s into playback
        # reloads the EasyEffects preset while audio is running, causing
        # an audible crack.
        if not peak_monitor_playback_armed:
            await sync_peak_monitor_for_playback_state(player_instance.state)
        else:
            logger.debug(
                "Peak monitor already armed; skipping redundant playback transition sync: source=%s url=%s",
                expected_track.get("source"), expected_track.get("url"),
            )


async def _sync_subwoofer_runtime_after_playback_transition(expected_track: dict | None, timeout_ms: int = 3200) -> None:
    if not expected_track or expected_track.get("source") not in {"local", "radio"}:
        return
    if subwoofer_runtime is None:
        return
    settled = await _wait_for_player_current_file(expected_track.get("url"), timeout_ms=timeout_ms)
    if not settled or not _current_track_matches(expected_track):
        return
    await asyncio.sleep(0.35)
    if not _current_track_matches(expected_track):
        return
    try:
        overview = get_audio_output_overview()
        output_mode = overview.get("output_mode") or {}
        if output_mode.get("mode") not in OUTPUT_MODE_SUBWOOFER_MODES:
            return
        before = subwoofer_runtime.snapshot()
        await _sync_subwoofer_runtime(overview)
        after = subwoofer_runtime.snapshot()
        before_config = before.get("config") or {}
        after_config = after.get("config") or {}
        logger.info(
            "Subwoofer runtime playback transition resync: source=%s sample_rate_before=%s sample_rate_after=%s "
            "active=%s last_error=%s",
            expected_track.get("source"),
            before_config.get("sample_rate"),
            after_config.get("sample_rate"),
            after.get("active"),
            after.get("last_error"),
        )
    except Exception as exc:
        logger.warning("Subwoofer runtime playback transition resync failed: %s", exc)



def _capture_playback_state_before_measurement():
    """Save playback state before measurement starts (radio + library/local).

    After measurement at 48 kHz, the active playback stream (paused/playing) is
    stale at the wrong sample rate. This capture enables a controlled restart
    on resume instead of a simple unpause.

    Stores source, url/path, id, title, expected_rate, position, and paused
    flag so the controlled restart can restore the user's exact spot.

    Side-effect: also fills _radio_state_before_measurement / radio_stream_stale
    so the existing radio-specific path in toggle_playback keeps working.
    """
    global _playback_state_before_measurement, _radio_state_before_measurement
    _playback_state_before_measurement = None
    _radio_state_before_measurement = None
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
    output_mode = dict(overview.get("output_mode") or {})
    selected_output = dict(overview.get("selected_output") or {})
    current_output = dict(overview.get("current_output") or {})
    output_mode["effective_output_rate"] = effective_rate
    if selected_output:
        selected_output["active_rate"] = effective_rate
    if current_output and current_output.get("key") == selected_output.get("key"):
        current_output["active_rate"] = effective_rate
    return {
        **overview,
        "output_mode": output_mode,
        "selected_output": selected_output or overview.get("selected_output"),
        "current_output": current_output or overview.get("current_output"),
    }


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
    snapshot = snapshot or {}
    config = snapshot.get("config") or {}
    return {
        "active": bool(snapshot.get("active")),
        "helper_pid": snapshot.get("helper_pid"),
        "sample_rate": config.get("sample_rate"),
        "sub_alignment_ms": config.get("sub_alignment_ms"),
        "main_delay_ms": config.get("derived_main_delay_ms"),
        "sub_delay_ms": config.get("derived_sub_delay_ms"),
        "stage": snapshot.get("stage"),
        "last_error": snapshot.get("last_error"),
    }


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


async def _prepare_subwoofer_runtime_for_measurement_start(measurement_rate: int) -> Optional[int]:
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
    restore_force_rate: Optional[int] = None
    if previous_force_rate != measurement_rate:
        restore_force_rate = int(previous_force_rate or 0)
        _set_pipewire_force_rate(measurement_rate)
        logger.info(
            "%s measurement samplerate pre-arm applied: target_rate=%s previous_active_rate=%s previous_force_rate=%s",
            mode_num,
            measurement_rate,
            previous_active_rate,
            previous_force_rate,
        )
    else:
        logger.info(
            "%s measurement samplerate pre-arm already active: target_rate=%s active_rate=%s force_rate=%s",
            mode_num,
            measurement_rate,
            previous_active_rate,
            previous_force_rate,
        )
    if previous_active_rate != measurement_rate:
        _pulse_suspend_sink_for_samplerate(output_key, "measurement-pre-arm")

    overview = _audio_output_overview_with_effective_rate(get_audio_output_overview(), measurement_rate)
    await _sync_subwoofer_runtime(overview)

    aligned, overview = await _wait_for_selected_output_effective_rate(measurement_rate, timeout_ms=3500)
    if not aligned:
        output_mode = overview.get("output_mode") or {}
        effective_rate = output_mode.get("effective_output_rate")
        if restore_force_rate is not None:
            _set_pipewire_force_rate(restore_force_rate)
        raise RuntimeError(
            f"{mode_num} measurement pre-arm failed: selected output did not reach "
            f"{measurement_rate} Hz before sweep start (effective_rate={effective_rate})"
        )

    await _sync_subwoofer_runtime(overview)
    after = subwoofer_runtime.snapshot()
    samplerate_after = get_samplerate_status()
    after_config = after.get("config") or {}
    runtime_config = SubwooferRuntimeConfig.from_overview(overview)
    helper_rate = after_config.get("sample_rate")
    if not after.get("active") or helper_rate != measurement_rate:
        if restore_force_rate is not None:
            _set_pipewire_force_rate(restore_force_rate)
        raise RuntimeError(
            f"{mode_num} measurement pre-arm failed: helper did not settle at "
            f"{measurement_rate} Hz before sweep start (active={after.get('active')} sample_rate={helper_rate})"
        )

    logger.info(
        "%s measurement helper pre-armed before sweep: target_rate=%s helper_rate_before=%s helper_rate_after=%s "
        "helper_pid=%s force_rate_restore=%s samplerate_after=%s helper_after=%s",
        mode_num,
        measurement_rate,
        (before.get("config") or {}).get("sample_rate"),
        helper_rate,
        after.get("helper_pid"),
        restore_force_rate,
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
    return restore_force_rate


async def _release_measurement_samplerate_force_after_job(job_id: str, expected_rate: int, restore_force_rate: int) -> None:
    global subwoofer_runtime, radio_stream_stale_after_measurement, playback_stream_stale_after_measurement
    logger.info(
        "Measurement samplerate release watcher started: job_id=%s expected_rate=%s restore_force_rate=%s",
        job_id,
        expected_rate,
        restore_force_rate,
    )
    for _ in range(300):
        await asyncio.sleep(0.5)
        if measurement_store is None:
            logger.info("Measurement samplerate release watcher stopped: job_id=%s measurement_store_missing=true", job_id)
            return
        try:
            job = measurement_store.get_job(job_id)
        except Exception as exc:
            logger.info("Measurement samplerate release watcher stopped: job_id=%s job_lookup_failed=%s", job_id, exc)
            return
        status = str(job.get("status") or "")
        if status in {"completed", "failed", "cancelled"}:
            try:
                await _dump_21_runtime_state(f"backend-before-release-{status}", {"job_id": job_id, "job_status": status})
                current_force_rate = _get_current_pipewire_force_rate()
                restore_value = restore_force_rate if restore_force_rate > 0 else 0
                if current_force_rate == expected_rate:
                    _set_pipewire_force_rate(restore_value)
                    logger.info(
                        "Measurement samplerate pre-arm released: job_id=%s previous_force_rate=%s status=%s",
                        job_id,
                        restore_force_rate,
                        status,
                    )
                else:
                    logger.info(
                        "Measurement samplerate pre-arm force release skipped: job_id=%s current_force_rate=%s expected_rate=%s status=%s",
                        job_id,
                        current_force_rate,
                        expected_rate,
                        status,
                    )
                # Re-sync subwoofer runtime at the restored playback rate even
                # if another playback repair already changed force-rate. The
                # helper may still be running at the measurement rate.
                if subwoofer_runtime is not None:
                    logger.info(
                        "Measurement samplerate release invoking _sync_subwoofer_runtime_at_rate: job_id=%s target_rate=%s current_force_rate=%s",
                        job_id,
                        restore_value,
                        current_force_rate,
                    )
                    await _sync_subwoofer_runtime_at_rate(restore_value)
                    await _dump_21_runtime_state(f"backend-after-release-resync-{status}", {"job_id": job_id, "job_status": status})
                else:
                    logger.info(
                        "Measurement samplerate release cannot re-sync: job_id=%s subwoofer_runtime_missing=true",
                        job_id,
                    )
            except Exception as exc:
                logger.warning("Measurement samplerate pre-arm release failed: job_id=%s error=%s", job_id, exc)
            # Mark playback stream as stale after measurement at 48 kHz.
            # This is generic (radio + library/local). If a playback stream was
            # active before measurement at a different rate, it is now stale.
            if _playback_state_before_measurement is not None:
                saved = _playback_state_before_measurement
                saved_rate = saved.get("expected_rate")
                if isinstance(saved_rate, int) and saved_rate > 0 and saved_rate != expected_rate:
                    playback_stream_stale_after_measurement = True
                    # Also set the radio-specific flag for the radio branch in toggle_playback
                    if saved.get("source") == "radio":
                        radio_stream_stale_after_measurement = True
                    logger.info(
                        "PLAYBACK-RESUME-DIAG measurement complete, marking playback stream stale: "
                        "source=%s url=%s expected_rate=%s measurement_rate=%s position=%.2f was_paused=%s",
                        saved.get("source"), saved.get("url"), saved_rate, expected_rate,
                        saved.get("position", 0), saved.get("was_paused", False),
                    )
                else:
                    logger.info(
                        "PLAYBACK-RESUME-DIAG saved playback rate matches measurement rate, no stale flag: "
                        "source=%s expected_rate=%s measurement_rate=%s",
                        saved.get("source"), saved_rate, expected_rate,
                    )
            return


async def _sync_subwoofer_runtime_at_rate(target_rate: int) -> None:
    """Re-sync the subwoofer runtime at a specific playback sample rate.

    Called after measurement completes to restore the helper at normal playback
    rate instead of leaving it at the measurement rate.

    When target_rate is 0 (no force-rate override), discovers the actual
    effective output rate and syncs at that rate.
    """
    global subwoofer_runtime
    if subwoofer_runtime is None:
        logger.info("Subwoofer runtime measurement release re-sync skipped: subwoofer_runtime_missing=true target_rate=%s", target_rate)
        return
    logger.info("Subwoofer runtime measurement release re-sync requested: raw_target_rate=%s", target_rate)
    if target_rate <= 0:
        # No force rate — discover the actual effective rate from the output
        overview = get_audio_output_overview()
        output_mode = overview.get("output_mode") or {}
        effective_rate = output_mode.get("effective_output_rate")
        if isinstance(effective_rate, int) and effective_rate > 0:
            target_rate = effective_rate
        else:
            selected = overview.get("selected_output") or {}
            sr = selected.get("active_rate")
            if isinstance(sr, int) and sr > 0:
                target_rate = sr
            else:
                target_rate = DEFAULT_SAMPLE_RATE
    overview = get_audio_output_overview()
    output_mode = overview.get("output_mode") or {}
    if output_mode.get("mode") not in OUTPUT_MODE_SUBWOOFER_MODES:
        logger.info(
            "Subwoofer runtime measurement release re-sync skipped: api_mode=%s target_rate=%s",
            output_mode.get("mode"),
            target_rate,
        )
        return
    overview = _audio_output_overview_with_effective_rate(overview, target_rate)
    await _sync_subwoofer_runtime(overview)
    # Wait briefly and sync again to ensure stable state
    await asyncio.sleep(0.5)
    overview = get_audio_output_overview()
    overview = _audio_output_overview_with_effective_rate(overview, target_rate)
    await _sync_subwoofer_runtime(overview)
    runtime_snapshot = subwoofer_runtime.snapshot()
    logger.info(
        "Subwoofer runtime measurement release re-sync: target_rate=%s active=%s helper_pid=%s",
        target_rate,
        runtime_snapshot.get("active"),
        runtime_snapshot.get("helper_pid"),
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
        try:
            await subwoofer_runtime.reclean_direct_easyeffects_links()
        except Exception as exc:
            logger.warning("Measurement release input repair failed: target_rate=%s error=%s", target_rate, exc)
            return
        await _dump_21_runtime_state(
            "backend-release-input-repair-after",
            {"target_rate": target_rate, "delay_s": delay},
        )


def _subwoofer_helper_input_links_present() -> bool:
    result = _run_debug_command(["pw-link", "-l"], 2.0)
    if result.get("returncode") != 0:
        return True
    text = result.get("stdout", "")
    return (
        _contains_link(text, "ee_soe_output_level:output_FL", "fxroute_21_stage1:input_L")
        and _contains_link(text, "ee_soe_output_level:output_FR", "fxroute_21_stage1:input_R")
    )


async def _subwoofer_runtime_link_watch_loop() -> None:
    while True:
        await asyncio.sleep(2.0)
        if subwoofer_runtime is None:
            continue
        try:
            overview = get_audio_output_overview()
            output_mode = overview.get("output_mode") or {}
            if output_mode.get("mode") not in OUTPUT_MODE_SUBWOOFER_MODES:
                continue
            if getattr(subwoofer_runtime, "sync_in_progress", False):
                continue
            snapshot = subwoofer_runtime.snapshot()
            input_links_present = _subwoofer_helper_input_links_present()
            direct_links_present = await subwoofer_runtime.direct_easyeffects_front_links_present()
            if snapshot.get("active") and input_links_present and not direct_links_present:
                continue
            if not snapshot.get("active") or not snapshot.get("helper_pid"):
                logger.info(
                    "Subwoofer link watch full resync applying: runtime_active=%s helper_pid=%s input_links_present=%s direct_links_present=%s",
                    snapshot.get("active"),
                    snapshot.get("helper_pid"),
                    input_links_present,
                    direct_links_present,
                )
                await _sync_subwoofer_runtime(overview)
                await _dump_21_runtime_state(
                    "backend-link-watch-full-resync-after",
                    {
                        "reason": "runtime-inactive-or-helper-missing",
                        "active_before": snapshot.get("active"),
                        "helper_pid_before": snapshot.get("helper_pid"),
                        "input_links_present_before": input_links_present,
                        "direct_links_present_before": direct_links_present,
                    },
                )
                continue
            logger.info(
                "Subwoofer link watch repair applying: input_links_present=%s direct_links_present=%s",
                input_links_present,
                direct_links_present,
            )
            await subwoofer_runtime.reclean_direct_easyeffects_links()
            await _dump_21_runtime_state(
                "backend-link-watch-repair-after",
                {
                    "reason": "direct-ee-to-hardware-present" if direct_links_present else "ee-helper-input-missing",
                    "input_links_present_before": input_links_present,
                    "direct_links_present_before": direct_links_present,
                },
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
) -> Optional[int]:
    rate = _get_player_audio_samplerate()
    if rate:
        return rate
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while time.monotonic() <= deadline:
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
        rate = _get_player_audio_samplerate()
        if rate:
            return rate
    return None


async def _resolve_expected_playback_samplerate(source: str) -> Optional[int]:
    rate = await _wait_for_player_audio_samplerate()
    if rate or source != "radio":
        return rate
    await asyncio.sleep(RADIO_SAMPLERATE_RENEGOTIATE_DELAY_MS / 1000)
    return await _wait_for_player_audio_samplerate(timeout_ms=1200)


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


async def _bounce_easyeffects_preset_for_samplerate_recovery(
    *,
    source_label: str,
    expected_rate: int,
    sink_rate: int,
    detail: str,
    still_valid,
) -> None:
    global easyeffects_manager, easyeffects_preset_load_lock
    if not easyeffects_manager:
        return

    active_preset = easyeffects_manager.get_active_preset()
    if not active_preset:
        return

    bounce_preset = "Neutral" if active_preset != "Neutral" else "Direct"
    logger.info(
        "%s samplerate mismatch detected, bouncing EasyEffects preset via %s: preset=%s expected_rate=%s sink_rate=%s detail=%s",
        source_label,
        bounce_preset,
        active_preset,
        expected_rate,
        sink_rate,
        detail,
    )

    if easyeffects_preset_load_lock is None:
        easyeffects_preset_load_lock = asyncio.Lock()

    async with easyeffects_preset_load_lock:
        if not still_valid():
            return
        easyeffects_manager.load_preset(bounce_preset)
        await asyncio.sleep(RADIO_SAMPLERATE_PRESET_BOUNCE_DELAY_MS / 1000)
        if not still_valid():
            return
        easyeffects_manager.load_preset(active_preset, convolver_sample_rate_hz=expected_rate)
        status = easyeffects_manager.get_status()
    await manager.broadcast({"type": "easyeffects", "data": status})
    await refresh_peak_monitor_after_effects_change(f"{source_label.lower()}-samplerate-mismatch-recovery")
    final_status = get_samplerate_status()
    logger.info(
        "%s samplerate mismatch recovery finished: preset=%s final_sink_rate=%s expected_rate=%s detail=%s",
        source_label,
        active_preset,
        final_status.get("active_rate"),
        expected_rate,
        detail,
    )


async def _maybe_recover_samplerate_mismatch(expected_track: dict | None) -> None:
    if not expected_track or expected_track.get("source") not in {"local", "radio"}:
        return
    if not easyeffects_manager or not player_instance or not player_instance._running:
        return

    await asyncio.sleep(RADIO_SAMPLERATE_RENEGOTIATE_DELAY_MS / 1000)

    if not _current_track_matches(expected_track):
        return

    mpv_rate = await _resolve_expected_playback_samplerate(expected_track.get("source") or "")
    if not mpv_rate:
        logger.info(
            "Skipping samplerate mismatch recovery because player samplerate is still unavailable: source=%s url=%s",
            expected_track.get("source"),
            expected_track.get("url"),
        )
        return

    try:
        samplerate_status = get_samplerate_status()
    except Exception as exc:
        logger.debug("Samplerate mismatch recovery check failed: %s", exc)
        return

    sink_rate = samplerate_status.get("active_rate")
    if not isinstance(sink_rate, int) or sink_rate <= 0:
        return

    if sink_rate == mpv_rate:
        return

    try:
        source = expected_track.get("source")
        if source in {"local", "radio"}:
            try:
                await _ensure_radio_samplerate_force(mpv_rate, f"{source}-samplerate-mismatch")
            except Exception as exc:
                logger.warning("Playback samplerate force-rate failed during mismatch recovery: %s", exc)
        logger.info(
            "Local/radio samplerate mismatch detected; syncing active EasyEffects preset without Neutral/Direct bounce: expected_rate=%s sink_rate=%s track=%s source=%s",
            mpv_rate,
            sink_rate,
            expected_track.get("url"),
            source,
        )
        await _sync_easyeffects_preset_for_playback_samplerate(
            sample_rate_hz=mpv_rate,
            reason="local-samplerate-mismatch",
            detail=f"sink_rate={sink_rate} track={expected_track.get('url')} source={expected_track.get('source')}",
        )
        await refresh_peak_monitor_after_effects_change("local-samplerate-mismatch")
    except Exception as exc:
        logger.warning("Samplerate mismatch recovery failed for %s: %s", expected_track.get("url"), exc)


async def _maybe_recover_spotify_samplerate_mismatch(
    delay_ms: int = RADIO_SAMPLERATE_RENEGOTIATE_DELAY_MS,
    reason: str = "unspecified",
) -> None:
    global last_spotify_samplerate_recovery_at, spotify_samplerate_recovery_lock, spotify_samplerate_recovery_active, latest_spotify_state
    try:
        spotify_state = await get_spotify_ui_state()
        latest_spotify_state = spotify_state
    except Exception:
        spotify_state = latest_spotify_state or {}
    logger.info(
        "Spotify samplerate recovery entry: reason=%s delay_ms=%s footer_owner=%s spotify_status=%s",
        reason,
        delay_ms,
        current_footer_owner,
        spotify_state.get("status"),
    )
    try:
        if spotify_samplerate_recovery_lock is None:
            spotify_samplerate_recovery_lock = asyncio.Lock()
        if spotify_samplerate_recovery_lock.locked():
            logger.info("Spotify samplerate recovery skipped: lock busy reason=%s", reason)
            return

        if not easyeffects_manager:
            logger.info("Spotify samplerate recovery skipped: no easyeffects_manager")
            return

        async with spotify_samplerate_recovery_lock:
            spotify_samplerate_recovery_active = True
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)

            spotify_inputs = _list_spotify_sink_inputs()
            spotify_rate = _get_first_sink_input_samplerate(spotify_inputs)
            if not spotify_rate:
                logger.info("Spotify samplerate recovery skipped: no spotify_rate (inputs=%s) reason=%s", len(spotify_inputs), reason)
                return

            spotify_state = await get_spotify_ui_state()
            latest_spotify_state = spotify_state
            if spotify_state.get("status") != "Playing":
                logger.info("Spotify samplerate recovery skipped: status=%s reason=%s", spotify_state.get("status"), reason)
                return

            try:
                samplerate_status = get_samplerate_status()
            except Exception as exc:
                logger.warning("Spotify samplerate recovery status check failed: %s", exc)
                return

            sink_rate = samplerate_status.get("active_rate")
            logger.info(
                "Spotify samplerate recovery probe: reason=%s footer_owner=%s spotify_inputs=%s spotify_rate=%s spotify_status=%s sink_rate=%s title=%s",
                reason,
                current_footer_owner,
                len(spotify_inputs),
                spotify_rate,
                spotify_state.get("status"),
                sink_rate,
                spotify_state.get("title"),
            )
            if not isinstance(sink_rate, int) or sink_rate <= 0:
                logger.info("Spotify samplerate recovery skipped: invalid sink_rate=%s spotify_rate=%s reason=%s", sink_rate, spotify_rate, reason)
                return
            if sink_rate == spotify_rate:
                logger.info("Spotify samplerate recovery not needed: sink_rate=%s spotify_rate=%s reason=%s", sink_rate, spotify_rate, reason)
                return

            now = time.monotonic()
            if now - last_spotify_samplerate_recovery_at < 4.0:
                logger.info("Spotify samplerate recovery cooldown active: sink_rate=%s spotify_rate=%s delta=%.3f reason=%s", sink_rate, spotify_rate, now - last_spotify_samplerate_recovery_at, reason)
                return
            last_spotify_samplerate_recovery_at = now

            logger.info("Spotify samplerate recovery proceeding: reason=%s sink_rate=%s spotify_rate=%s footer_owner=%s title=%s", reason, sink_rate, spotify_rate, current_footer_owner, spotify_state.get("title"))
            aligned = False
            restart_stream_rate = spotify_rate
            restart_sink_rate = sink_rate
            if reason.startswith("watcher:"):
                logger.info(
                    "Spotify samplerate recovery stage 1 skipped for watcher-confirmed mismatch: reason=%s sink_rate=%s spotify_rate=%s",
                    reason,
                    sink_rate,
                    spotify_rate,
                )
            else:
                logger.info("Spotify samplerate recovery stage 1: controlled Spotify start/stop")
                try:
                    aligned, restart_stream_rate, restart_sink_rate = await asyncio.wait_for(
                        _recover_spotify_samplerate_alignment(),
                        timeout=max(3.5, (PIPEWIRE_HANDOFF_RELEASE_TIMEOUT_MS / 1000) * 2.5),
                    )
                except asyncio.TimeoutError:
                    aligned, restart_stream_rate, restart_sink_rate = False, None, None
                    logger.warning(
                        "Spotify samplerate recovery stage 1 timed out: reason=%s sink_rate=%s spotify_rate=%s title=%s",
                        reason,
                        sink_rate,
                        spotify_rate,
                        spotify_state.get("title"),
                    )
                logger.info(
                    "Spotify samplerate recovery stage 1 result: reason=%s aligned=%s stream_rate=%s sink_rate=%s",
                    reason,
                    aligned,
                    restart_stream_rate,
                    restart_sink_rate,
                )
                if aligned:
                    return
                sink_rate = restart_sink_rate if isinstance(restart_sink_rate, int) and restart_sink_rate > 0 else sink_rate
                spotify_rate = restart_stream_rate if isinstance(restart_stream_rate, int) and restart_stream_rate > 0 else spotify_rate

            logger.info("Spotify samplerate recovery stage 2: EasyEffects preset bounce fallback")
            await _bounce_easyeffects_preset_for_samplerate_recovery(
                source_label="Spotify",
                expected_rate=spotify_rate,
                sink_rate=sink_rate,
                detail=f"title={spotify_state.get('title')} artist={spotify_state.get('artist')}",
                still_valid=lambda: bool(_list_spotify_sink_inputs()),
            )
    except Exception as exc:
        logger.warning("Spotify samplerate mismatch recovery failed: %s", exc)
    finally:
        spotify_samplerate_recovery_active = False


def _prepare_local_queue(track_id: str, queue_track_ids: Optional[list[str]] = None, shuffle: bool = False, loop: bool = False):
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

    selected_ids = [track_id]
    if queue_track_ids:
        selected_ids = [candidate for candidate in queue_track_ids if candidate in tracks_by_id]
        if track_id not in selected_ids:
            selected_ids.insert(0, track_id)

    ordered_tracks = [tracks_by_id[track.id].to_dict() for track in tracks if track.id in set(selected_ids)]
    if not ordered_tracks:
        raise HTTPException(status_code=404, detail="Track not found")

    original_tracks = [dict(track) for track in ordered_tracks]

    if shuffle and len(ordered_tracks) > 1:
        current_track = next((track for track in ordered_tracks if track.get("id") == track_id), ordered_tracks[0])
        remaining = [track for track in ordered_tracks if track.get("id") != current_track.get("id")]
        random.shuffle(remaining)
        ordered_tracks = [current_track] + remaining

    playback_queue = ordered_tracks if len(ordered_tracks) > 1 else []
    playback_queue_original = original_tracks if len(original_tracks) > 1 else []
    playback_queue_mode = "mpv_native" if _should_use_mpv_native_queue(playback_queue) else "app_replace"
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
    global queue_transition_target_url
    if len(playback_queue) <= 1:
        return False
    if index < 0 or index >= len(playback_queue):
        return False

    next_track = dict(playback_queue[index])
    next_url = next_track.get("url")
    if not next_url:
        _clear_playback_queue()
        return False

    player_state = player_instance.state
    previous_track_context = dict(current_track_info or {})
    previous_file = player_state.get("current_file") or previous_track_context.get("url")
    previous_source = previous_track_context.get("source")

    synced_track = _sync_track_context_from_queue_index(index)
    if not synced_track:
        return False

    if playback_queue_mode == "mpv_native":
        queue_transition_target_url = next_url
        try:
            player_instance.set_playlist_pos(index)
            player_instance.set_pause(False)
            _record_local_track_started(synced_track)
            return True
        except Exception:
            queue_transition_target_url = None
            raise

    apply_hard_handoff, handoff_reason = _should_apply_hard_handoff_for_requested_play(
        requested_source="local",
        previous_source=previous_source,
        previous_file=previous_file,
        next_url=next_url,
    )
    if apply_hard_handoff:
        await _apply_hard_playback_handoff(previous_file, next_url, handoff_reason, transition_reason)

    prearm_rate, prearm_generation = await _prearm_known_local_samplerate(next_track, f"{transition_reason}:queue")
    queue_transition_target_url = next_url
    try:
        player_instance.loadfile(next_url, mode="replace")
        if prearm_rate and prearm_generation:
            asyncio.create_task(_release_local_samplerate_prearm(prearm_rate, prearm_generation, f"{transition_reason}:queue"))
        # Sync 2.1 helper at pre-armed rate before audio becomes audible
        if subwoofer_runtime is not None and prearm_rate is not None:
            await _sync_subwoofer_runtime(get_audio_output_overview())
        player_instance.set_pause(False)
        _record_local_track_started(next_track)
        asyncio.create_task(_maybe_recover_samplerate_mismatch(next_track.copy()))
        return True
    except Exception:
        queue_transition_target_url = None
        raise


async def _advance_playback_queue(*, transition_reason: str = "queue advance") -> bool:
    global playback_queue_index
    if len(playback_queue) <= 1:
        return False
    next_index = playback_queue_index + 1
    if next_index >= len(playback_queue):
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


def _set_queue_shuffle(enabled: bool) -> bool:
    global playback_queue, playback_queue_original, playback_queue_index, playback_queue_shuffle
    if len(playback_queue) <= 1:
        playback_queue_shuffle = False
        return False
    playback_queue_shuffle = bool(enabled)
    current_index = playback_queue_index if 0 <= playback_queue_index < len(playback_queue) else 0
    current_track_id = (playback_queue[current_index] or {}).get("id") if playback_queue else None
    if enabled:
        current_track = dict(playback_queue[current_index])
        remaining = [dict(track) for track in playback_queue if track.get("id") != current_track_id]
        random.shuffle(remaining)
        playback_queue = [current_track] + remaining
        playback_queue_index = 0
        if playback_queue_mode == "mpv_native" and player_instance and player_instance._running:
            _prime_mpv_native_queue(playback_queue_index)
    elif playback_queue_original:
        playback_queue = [dict(track) for track in playback_queue_original]
        if current_track_id:
            playback_queue_index = next((index for index, track in enumerate(playback_queue) if track.get("id") == current_track_id), current_index)
        if playback_queue_mode == "mpv_native" and player_instance and player_instance._running:
            _prime_mpv_native_queue(playback_queue_index if playback_queue_index >= 0 else 0)
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
        if playback_queue_mode == "mpv_native" and player_instance and player_instance._running:
            player_instance.set_loop_playlist(playback_queue_loop)
        single_track_loop = False
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

    if playback_queue_mode == "mpv_native" and len(playback_queue) > 1:
        _trim_mpv_native_queue_to_current()

    track_info = _prepare_local_queue(
        current_track["id"],
        queue_track_ids,
        shuffle=shuffle,
        loop=loop,
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
    try:
        return get_output_volume()
    except Exception as exc:
        logger.warning("Failed to read output volume, using fallback %s: %s", default, exc)
        return default


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
        "target": None,
        "last_over_at": None,
        "last_error": None,
    }

    # Keep playback/status payloads lightweight. EasyEffects has dedicated
    # endpoints and websocket updates, and pulling full EasyEffects status here
    # can stall frequent /api/status polling during playback.
    return playback_state


async def on_peak_monitor_change(snapshot: dict):
    await manager.broadcast({"type": "playback_peak_warning", "data": snapshot})


async def sync_peak_monitor_for_playback_state(state: dict):
    global peak_monitor_playback_armed, peak_monitor, peak_monitor_transition_lock, peak_monitor_context_signature, current_track_info
    if not peak_monitor:
        return
    if peak_monitor_transition_lock is None:
        peak_monitor_transition_lock = asyncio.Lock()
    async with peak_monitor_transition_lock:
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
                expected_rate = await _resolve_expected_playback_samplerate(source) if source in {"local", "radio"} else None
                aligned = await _wait_for_samplerate_alignment(expected_rate) if expected_rate else False
                if source in {"local", "radio"} and expected_rate and not aligned:
                    try:
                        aligned = await _ensure_radio_samplerate_force(expected_rate, f"peak-monitor-playback-transition:{source}")
                    except Exception as exc:
                        logger.warning("Playback samplerate force-rate failed during playback transition: %s", exc)
                elif source not in {"local", "radio"}:
                    _clear_radio_samplerate_force_if_active(f"playback-transition:{source}")
                if not aligned:
                    await asyncio.sleep(PEAK_MONITOR_RESTART_SETTLE_MS / 1000)
                if expected_rate:
                    try:
                        await _sync_easyeffects_preset_for_playback_samplerate(
                            sample_rate_hz=expected_rate,
                            reason="peak-monitor-playback-transition",
                            detail=f"signature={desired_signature} aligned={aligned}",
                        )
                    except Exception as exc:
                        logger.warning("EasyEffects playback samplerate preset sync failed before peak monitor restart: %s", exc)
                await _ensure_stereo_easyeffects_output_graph()
                logger.info(
                    "Restarting peak monitor on playback context change to refresh PipeWire links: %s (expected_rate=%s aligned=%s)",
                    desired_signature,
                    expected_rate,
                    aligned,
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
            _clear_radio_samplerate_force_if_active("playback-inactive")
            peak_monitor_playback_armed = False
            # peak_monitor_context_signature is preserved for same-source resume detection.


async def sync_peak_monitor_for_spotify_state(data: dict):
    global peak_monitor_playback_armed, peak_monitor, player_instance, peak_monitor_transition_lock, peak_monitor_context_signature, spotify_samplerate_recovery_active
    if not peak_monitor:
        return
    if peak_monitor_transition_lock is None:
        peak_monitor_transition_lock = asyncio.Lock()

    async with peak_monitor_transition_lock:
        player_state = player_instance.state if player_instance else {}
        is_spotify_playing = _is_spotify_playback_active(data)
        desired_signature = "spotify:playing" if is_spotify_playing else None

        if is_spotify_playing and (not peak_monitor_playback_armed or peak_monitor_context_signature != desired_signature):
            if spotify_samplerate_recovery_active:
                logger.info("Delaying peak monitor restart while Spotify samplerate recovery is active")
                return
            await _prearm_spotify_samplerate("spotify-peak-monitor-sync")
            aligned, spotify_rate, sink_rate = await _wait_for_pipewire_spotify_samplerate_alignment(
                timeout_ms=PEAK_MONITOR_RATE_MATCH_TIMEOUT_MS,
            )
            if not aligned:
                logger.info(
                    "Deferring peak monitor restart for Spotify until samplerate aligns (spotify_rate=%s sink_rate=%s)",
                    spotify_rate,
                    sink_rate,
                )
                return
            peak_monitor_playback_armed = True
            peak_monitor_context_signature = desired_signature
            logger.info(
                "Starting peak monitor for active Spotify playback (aligned=%s spotify_rate=%s sink_rate=%s)",
                aligned,
                spotify_rate,
                sink_rate,
            )
            await peak_monitor.restart()
            await manager.broadcast({"type": "playback_peak_warning", "data": peak_monitor.snapshot()})
        elif (
            not is_spotify_playing
            and peak_monitor_playback_armed
            and str(peak_monitor_context_signature or "").startswith("spotify:")
        ):
            if spotify_samplerate_recovery_active:
                logger.info("Keeping peak monitor armed while Spotify samplerate recovery is active")
                return
            await asyncio.sleep(PEAK_MONITOR_INACTIVE_GRACE_MS / 1000)
            refreshed_player_state = player_instance.state if player_instance else {}
            refreshed_spotify_state = await get_spotify_ui_state()
            if spotify_samplerate_recovery_active:
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



async def _radio_reconnect_after_delay(track_info: dict, attempt: int) -> None:
    global radio_reconnect_task
    try:
        await asyncio.sleep(RADIO_RECONNECT_DELAY_SECONDS)
        expected_url = (track_info or {}).get("url")
        if not expected_url:
            return
        if not player_instance or not player_instance._running:
            return
        if not current_track_info or current_track_info.get("source") != "radio" or current_track_info.get("url") != expected_url:
            return
        state = player_instance.state
        if state.get("current_file") and not state.get("ended"):
            return
        logger.info("Reconnecting radio stream after unexpected end: station=%s attempt=%s/%s", track_info.get("title") or track_info.get("id"), attempt, RADIO_RECONNECT_MAX_ATTEMPTS)
        await _wait_for_pipewire_mpv_release()
        player_instance.loadfile(expected_url, mode="replace")
        player_instance.set_pause(False)
        _mark_player_state_authoritative(player_instance.state)
        asyncio.create_task(_maybe_recover_samplerate_mismatch(track_info.copy()))
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
    radio_reconnect_task = asyncio.create_task(_radio_reconnect_after_delay(dict(track_info), radio_reconnect_attempts))


# Callback functions
def _mark_player_state_authoritative(state: dict | None) -> None:
    global latest_player_state_seq_seen
    seq = (state or {}).get("_seq")
    if isinstance(seq, int):
        latest_player_state_seq_seen = max(latest_player_state_seq_seen, seq)


async def on_player_state_change(state: dict):
    global queue_advancing, playback_queue_index, current_track_info, last_track_info, queue_transition_target_url, latest_player_state_seq_seen
    seq = state.get("_seq")
    if isinstance(seq, int):
        if seq < latest_player_state_seq_seen:
            return
        latest_player_state_seq_seen = seq

    if queue_transition_target_url:
        current_file = state.get("current_file")
        if current_file == queue_transition_target_url and not state.get("ended"):
            queue_transition_target_url = None
        elif current_file and current_file != queue_transition_target_url:
            queue_transition_target_url = None

    if playback_queue_mode == "mpv_native" and len(playback_queue) > 1:
        native_index = state.get("playlist_pos")
        if not isinstance(native_index, int):
            current_file = state.get("current_file")
            native_index = next((idx for idx, item in enumerate(playback_queue) if item.get("url") == current_file), None)
        if isinstance(native_index, int) and 0 <= native_index < len(playback_queue):
            if playback_queue_index != native_index or (current_track_info or {}).get("id") != playback_queue[native_index].get("id"):
                playback_queue_index = native_index
                current_track_info = dict(playback_queue[native_index])
                last_track_info = dict(playback_queue[native_index])

    if (
        not queue_advancing
        and not queue_transition_target_url
        and state.get("ended")
        and not state.get("current_file")
        and current_track_info
        and current_track_info.get("source") == "local"
    ):
        queue_advancing = True
        try:
            if len(playback_queue) > 1 and await _advance_playback_queue(transition_reason="queue auto-advance"):
                return
            if single_track_loop and current_track_info and current_track_info.get("url"):
                await _wait_for_pipewire_mpv_release()
                prearm_rate, prearm_generation = await _prearm_known_local_samplerate(current_track_info, "single-track-loop")
                player_instance.loadfile(current_track_info["url"], mode="replace")
                if prearm_rate and prearm_generation:
                    asyncio.create_task(_release_local_samplerate_prearm(prearm_rate, prearm_generation, "single-track-loop"))
                # Sync 2.1 helper at pre-armed rate before audio becomes audible
                if subwoofer_runtime is not None and prearm_rate is not None:
                    await _sync_subwoofer_runtime(get_audio_output_overview())
                return
        finally:
            queue_advancing = False

    _schedule_radio_reconnect_if_needed(state)
    await sync_peak_monitor_for_playback_state(state)
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


async def _sync_subwoofer_runtime(audio_overview: dict | None = None) -> dict:
    global subwoofer_runtime
    overview = audio_overview or get_audio_output_overview()

    if subwoofer_runtime is None:
        return overview

    config = SubwooferRuntimeConfig.from_overview(overview)

    await subwoofer_runtime.sync(config)

    if config.output_mode in OUTPUT_MODE_SUBWOOFER_MODES:
        runtime_snapshot = subwoofer_runtime.snapshot()
        mode_num = "2.2 Stereo Bass" if config.output_mode == OUTPUT_MODE_SUBWOOFER_22_STEREO else "2.2" if config.output_mode == OUTPUT_MODE_SUBWOOFER_22 else "2.1"
        logger.info(
            "%s runtime sync: output_mode=%s runtime_active=%s "
            "hardware_output=%s device_channel_count=%s "
            "crossover_hz=%s main_highpass=%s "
            "sub1_level_db=%.1f sub1_alignment_ms=%.1f sub1_polarity=%s "
            "sub2_level_db=%.1f sub2_alignment_ms=%.1f sub2_polarity=%s "
            "derived_main_delay_ms=%.1f derived_sub1_delay_ms=%.1f derived_sub2_delay_ms=%.1f",
            mode_num,
            config.output_mode,
            runtime_snapshot.get("active"),
            config.output_key,
            config.output_channels,
            config.crossover_frequency_hz,
            config.main_highpass_enabled,
            config.sub_level_db,
            config.sub_alignment_ms,
            config.sub_polarity,
            config.sub2_level_db,
            config.sub2_alignment_ms,
            config.sub2_polarity,
            config.derived_main_delay_ms,
            config.derived_sub1_delay_ms,
            config.derived_sub2_delay_ms,
        )
    else:
        logger.info("Subwoofer runtime sync: output_mode=%s; stereo path unchanged", OUTPUT_MODE_STEREO)
        await _ensure_stereo_easyeffects_output_graph(overview)
    return overview


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
        for index, delay_s in enumerate(burst_delays):
            if delay_s > 0:
                await asyncio.sleep(delay_s if index == 0 else max(0.0, delay_s - burst_delays[index - 1]))
            spotify_inputs = _list_spotify_sink_inputs()
            spotify_rate = _get_first_sink_input_samplerate(spotify_inputs)
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
                if spotify_rate != sink_rate:
                    logger.info(
                        "Spotify detect watcher burst hit: reason=%s probe=%s/%s spotify_rate=%s sink_rate=%s title=%s",
                        reason,
                        index + 1,
                        len(burst_delays),
                        spotify_rate,
                        sink_rate,
                        spotify_state.get("title"),
                    )
                    logger.warning(
                        "Spotify mismatch detected by watcher: reason=%s spotify_rate=%s sink_rate=%s title=%s",
                        reason,
                        spotify_rate,
                        sink_rate,
                        spotify_state.get("title"),
                    )
                    await _maybe_recover_spotify_samplerate_mismatch(delay_ms=0, reason=f"watcher:{reason}")
                    break
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
    now = time.monotonic()
    if now - spotify_playerctl_last_trigger_at < 1.0:
        return
    spotify_playerctl_last_trigger_at = now
    if spotify_playerctl_detect_task and not spotify_playerctl_detect_task.done():
        spotify_playerctl_detect_task.cancel()
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
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1)
                except Exception:
                    proc.kill()
            raise
        except Exception as exc:
            logger.warning("Spotify playerctl watch loop failed: %s", exc)
        finally:
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1)
                except Exception:
                    proc.kill()
        await asyncio.sleep(1.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown."""
    global settings, player_instance, library_scanner, downloader, easyeffects_manager, measurement_store, peak_monitor, subwoofer_runtime, subwoofer_runtime_link_watch_task, hardware_controller, peak_monitor_playback_armed, peak_monitor_transition_lock, peak_monitor_context_signature, easyeffects_preset_load_lock, source_transition_lock, external_input_loopback_module_id, external_input_loopback_source_name, bluetooth_input_source_name, bluetooth_monitor_task, bluetooth_agent_process, spotify_playerctl_watch_task, spotify_playerctl_detect_task, spotify_state_refresh_task, spotify_state_poll_task, spotify_playerctl_last_trigger_at, spotify_samplerate_recovery_lock, spotify_samplerate_recovery_active, current_source_mode, latest_spotify_state

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
        logger.info("EasyEffects manager initialized")

        measurement_store = MeasurementStore()
        logger.info("Measurement store initialized: %s", measurement_store.measurements_dir)

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
        spotify_samplerate_recovery_lock = asyncio.Lock()
        spotify_samplerate_recovery_active = False
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

def _station_art_url_if_available(url: Optional[str]) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    if value.startswith("/static/station-art/"):
        art_path = (STATIC_DIR / "station-art" / Path(value).name).resolve()
        if not _path_within_root(art_path, STATIC_DIR / "station-art") or not art_path.is_file():
            return ""
    return value


def _station_api_payload(station):
    image_url = _station_art_url_if_available(station.image_url)
    custom_image_url = _station_art_url_if_available(station.custom_image_url)
    cached_custom_image_url = _station_art_url_if_available(getattr(station, "cached_custom_image_url", None))
    return {
        "id": station.id,
        "title": station.name,
        "image": cached_custom_image_url or custom_image_url or image_url or "",
        "image_url": image_url,
        "custom_image_url": custom_image_url,
        "cached_custom_image_url": cached_custom_image_url,
        "stream_url": station.stream_url,
        "input_url": station.input_url or station.stream_url,
        "artist": "Radio",
    }


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
        tmp = cached.with_suffix(cached.suffix + ".tmp")
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


@app.get("/api/stations")
async def list_stations():
    return [_station_api_payload(station) for station in get_stations()]


@app.post("/api/stations")
async def create_station(req: StationUpsertRequest):
    try:
        station = add_station(req.name, req.stream_url, req.custom_image_url)
        return {
            "status": "ok",
            "station": _station_api_payload(station),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/stations/{station_id}")
async def edit_station(station_id: str, req: StationUpsertRequest):
    try:
        station = update_station(station_id, req.name, req.stream_url, req.custom_image_url)
        return {
            "status": "ok",
            "station": _station_api_payload(station),
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/stations/{station_id}")
async def remove_station(station_id: str):
    try:
        delete_station(station_id)
        return {"status": "ok", "deleted": station_id}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

class StationImportItem(BaseModel):
    name: str = ""
    url: str = ""
    logo: str = ""
    genre: str = ""

@app.post("/api/stations/import")
async def import_stations(items: list[StationImportItem]):
    results = []
    for item in items:
        name = (item.name or "").strip()
        stream_url = (item.url or "").strip()
        custom_image_url = (item.logo or "").strip()
        if not stream_url:
            results.append({"status": "skipped", "reason": "missing url", "name": name})
            continue
        if not name:
            from urllib.parse import urlparse
            parsed = urlparse(stream_url)
            name = parsed.netloc or "Unknown Station"
        try:
            station = add_station(name, stream_url, custom_image_url)
            results.append({"status": "ok", "name": name})
        except ValueError as e:
            results.append({"status": "error", "name": name, "reason": str(e)})
    return {"results": results}

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
    source = req.source
    track_id = req.track_id
    url = req.url
    queue_track_ids = req.queue_track_ids or []
    global player_instance, current_track_info, last_track_info, last_radio_track_info, source_transition_lock, current_footer_owner, radio_reconnect_attempts, radio_reconnect_url, radio_reconnect_active_since, playback_stream_stale_after_measurement, _playback_state_before_measurement, radio_stream_stale_after_measurement, _radio_state_before_measurement
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if not _can_send_play_command():
        state = player_instance.state
        return {
            "status": "playing",
            "url": state.get("current_file") or "",
            "track": last_track_info or {},
            "playback": build_playback_payload(state),
        }
    if source_transition_lock is None:
        source_transition_lock = asyncio.Lock()
    # PATCH 1: explicit user-play invalidates any stale-after-measurement
    # snapshot. The user has chosen a new track/station; the old snapshot
    # is from a different track and would otherwise cause toggle_playback
    # to reload the wrong file on the next pause/play.
    if playback_stream_stale_after_measurement or _playback_state_before_measurement is not None:
        logger.info(
            "PLAYBACK-RESUME-DIAG stale_snapshot_invalidated_due_to_explicit_play "
            "reason=user_explicit_play source=%s track_id=%s",
            source, track_id,
        )
        playback_stream_stale_after_measurement = False
        _playback_state_before_measurement = None
    if radio_stream_stale_after_measurement or _radio_state_before_measurement is not None:
        logger.info(
            "RADIO-RESUME-DIAG stale_snapshot_invalidated_due_to_explicit_play "
            "reason=user_explicit_play track_id=%s",
            track_id,
        )
        radio_stream_stale_after_measurement = False
        _radio_state_before_measurement = None
    try:
        async with source_transition_lock:
            # Source exclusivity: pause Spotify and broadcast the new Spotify state
            current_footer_owner = "local"
            await pause_spotify_for_local_playback_broadcast()
            play_url = url
            track_info = {"id": track_id, "title": track_id, "artist": "", "source": source, "url": play_url}

            if source == "radio":
                radio_reconnect_attempts = 0
                radio_reconnect_url = None
                radio_reconnect_active_since = 0.0
                _clear_playback_queue()
                stations = get_stations()
                for s in stations:
                    if s.id == track_id:
                        play_url = s.stream_url
                        track_info = {"id": f"radio_{s.id}", "title": s.name, "artist": "Radio", "source": "radio", "url": s.stream_url}
                        break
            else:
                _clear_playback_queue()
                track_info = _prepare_local_queue(track_id, queue_track_ids, shuffle=req.shuffle, loop=req.loop)
                play_url = track_info.get("url")

            if not play_url:
                raise HTTPException(status_code=404, detail="Track not found")

            player_state = player_instance.state
            previous_file = player_state.get("current_file")
            previous_source = (current_track_info or {}).get("source")
            same_source = previous_file == play_url
            apply_hard_handoff, handoff_reason = _should_apply_hard_handoff_for_requested_play(
                requested_source=source,
                previous_source=previous_source,
                previous_file=previous_file,
                next_url=play_url,
            )

            if playback_queue_mode != "mpv_native":
                _reset_mpv_loop_state()

            resume_same_source = (
                same_source
                and player_state.get("paused")
                and player_state.get("current_file")
                and not player_state.get("ended")
                and not (playback_queue_mode == "mpv_native" and len(playback_queue) > 1)
            )

            if resume_same_source:
                player_instance.set_pause(False)
            else:
                if apply_hard_handoff:
                    await _apply_hard_playback_handoff(previous_file, play_url, handoff_reason, "play")
                if playback_queue_mode == "mpv_native" and len(playback_queue) > 1:
                    prearm_rate, prearm_generation = await _prearm_known_local_samplerate(track_info, "play:mpv-native-queue")
                    # Sync 2.1 helper at pre-armed rate before audio starts
                    if subwoofer_runtime is not None and prearm_rate is not None:
                        await _sync_subwoofer_runtime(get_audio_output_overview())
                    if not _prime_mpv_native_queue(playback_queue_index):
                        raise HTTPException(status_code=500, detail="Failed to initialize native mpv playlist")
                    if prearm_rate and prearm_generation:
                        asyncio.create_task(_release_local_samplerate_prearm(prearm_rate, prearm_generation, "play:mpv-native-queue"))
                else:
                    prearm_rate, prearm_generation = await _prearm_known_local_samplerate(track_info, "play")
                    # Radio: ensure sample rate before loadfile
                    if source == "radio":
                        try:
                            radio_rate = await _resolve_expected_playback_samplerate("radio")
                            if radio_rate:
                                await _ensure_radio_samplerate_force(radio_rate, "radio-start-before-loadfile")
                        except Exception as exc:
                            logger.warning(
                                "Pre-loadfile radio sample-rate apply failed: %s", exc,
                            )
                    player_instance.loadfile(play_url, mode="replace")
                    if prearm_rate and prearm_generation:
                        asyncio.create_task(_release_local_samplerate_prearm(prearm_rate, prearm_generation, "play"))
                    # Sync 2.1 helper at pre-armed rate before audio becomes audible
                    if subwoofer_runtime is not None and prearm_rate is not None:
                        await _sync_subwoofer_runtime(get_audio_output_overview())
                    # Ensure MPV is unpaused after loadfile (it may stay paused if previously paused by Spotify)
                    player_instance.set_pause(False)

            current_track_info = track_info
            last_track_info = track_info
            if source == "radio":
                last_radio_track_info = dict(track_info)
            if source == "local":
                _record_local_track_started(track_info)
            _mark_player_state_authoritative(player_instance.state)

            if source in {"local", "radio"}:
                asyncio.create_task(_sync_peak_monitor_after_playback_transition(track_info.copy()))
                asyncio.create_task(_maybe_recover_samplerate_mismatch(track_info.copy()))
                asyncio.create_task(_sync_subwoofer_runtime_after_playback_transition(track_info.copy()))
                state_seq = player_instance.state.get("_seq") if isinstance(player_instance.state, dict) else None
                _schedule_silent_active_watch(
                    source=source,
                    signature=f"player:{source}:{play_url}",
                    track=track_info.copy(),
                )

            return {
                "status": "playing",
                "url": play_url,
                "track": track_info,
                "playback": build_playback_payload(player_instance.state),
            }
    except MPVError as e:
        logger.error(f"Playback failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Playback error: {e}")
        raise HTTPException(status_code=500, detail="Playback failed")

@app.post("/api/pause")
async def pause_playback():
    global player_instance
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")

    state = player_instance.state
    if not state.get("current_file") or state.get("ended"):
        raise HTTPException(status_code=409, detail="Nothing is currently loaded to pause or resume")

    player_instance.pause()
    new_state = player_instance.state
    _mark_player_state_authoritative(new_state)
    return {
        "status": "paused" if new_state.get("paused") else "playing",
        "playback": build_playback_payload(new_state),
    }

@app.post("/api/playback/toggle")
async def toggle_playback():
    global player_instance, current_track_info, last_track_info, last_radio_track_info, radio_reconnect_attempts, radio_reconnect_url, radio_reconnect_active_since, radio_stream_stale_after_measurement, _radio_state_before_measurement, playback_stream_stale_after_measurement, _playback_state_before_measurement
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if not _can_send_play_command():
        state = player_instance.state
        return {"status": "paused" if state.get("paused") else "playing", "playback": build_playback_payload(state)}

    state = player_instance.state
    if state.get("current_file") and not state.get("ended"):
        was_paused = bool(state.get("paused"))
        prearm_rate = None
        prearm_generation = None
        # PATCH 2: stale-snapshot/current-track mismatch guard.
        # If a stale snapshot exists for a DIFFERENT track than the current
        # current_track_info, the user has switched tracks since the snapshot
        # was taken. The snapshot is from a previous measurement and must not
        # be used to reload an old file on the next pause/play. Invalidate it
        # here so the normal pause/resume path takes over (which works
        # correctly when state.current_file == current_track_info.url).
        if (current_track_info or {}).get("source") == "local" and playback_stream_stale_after_measurement:
            snap = _playback_state_before_measurement or {}
            snap_url = snap.get("url") or snap.get("path")
            current_url = (current_track_info or {}).get("url") or (current_track_info or {}).get("path")
            if snap_url and current_url and snap_url != current_url:
                logger.warning(
                    "PLAYBACK-RESUME-DIAG stale_snapshot_invalidated_due_to_current_track_mismatch "
                    "snapshot_url=%s current_track_url=%s source=local reason=user_switched_track",
                    snap_url, current_url,
                )
                playback_stream_stale_after_measurement = False
                _playback_state_before_measurement = None
        if (current_track_info or {}).get("source") == "radio" and radio_stream_stale_after_measurement:
            snap = _radio_state_before_measurement or {}
            snap_url = snap.get("url")
            current_url = (current_track_info or {}).get("url")
            if snap_url and current_url and snap_url != current_url:
                logger.warning(
                    "RADIO-RESUME-DIAG stale_snapshot_invalidated_due_to_current_track_mismatch "
                    "snapshot_url=%s current_track_url=%s source=radio reason=user_switched_station",
                    snap_url, current_url,
                )
                radio_stream_stale_after_measurement = False
                _radio_state_before_measurement = None
        if was_paused and _playback_state_matches_track(state, current_track_info):
            source = (current_track_info or {}).get("source")
            if source in {"local", "radio"}:
                await pause_spotify_for_local_playback_broadcast()

                # Local (library) playback stream is stale after measurement at 48 kHz.
                # Instead of unpausing the old stream, do a controlled restart
                # at the track's native sample rate, then seek back to saved position.
                if source == "local" and playback_stream_stale_after_measurement:
                    saved = _playback_state_before_measurement or {}
                    # Use ONLY the saved snapshot for restart inputs (do NOT touch
                    # current_track_info after stop_playback - it may be cleared).
                    saved_url = saved.get("url") or saved.get("path")
                    saved_expected_rate = saved.get("expected_rate")
                    saved_position = float(saved.get("position", 0) or 0)
                    if not saved_url:
                        # Missing data: clear stale flag to avoid blocking
                        playback_stream_stale_after_measurement = False
                        _playback_state_before_measurement = None
                        logger.warning(
                            "PLAYBACK-RESUME-DIAG controlled_restart_failed missing_local_url source=local",
                        )
                    elif not isinstance(saved_expected_rate, int) or saved_expected_rate <= 0:
                        # Missing rate: clear stale flag, allow normal resume
                        playback_stream_stale_after_measurement = False
                        _playback_state_before_measurement = None
                        logger.warning(
                            "PLAYBACK-RESUME-DIAG controlled_restart_failed missing_expected_rate source=local url=%s",
                            saved_url,
                        )
                    else:
                        local_url = saved_url
                        local_expected_rate = saved_expected_rate
                        # Build a minimal track_info dict for _prearm_known_local_samplerate
                        # (avoid using current_track_info which may be cleared after stop)
                        saved_track_info = {
                            "source": "local",
                            "url": local_url,
                            "path": saved.get("path") or local_url,
                            "id": saved.get("id", ""),
                            "title": saved.get("title", ""),
                            "sample_rate_hz": local_expected_rate,
                        }
                        old_state = player_instance.state
                        old_file = old_state.get("current_file", "")
                        old_paused = bool(old_state.get("paused"))
                        old_ended = bool(old_state.get("ended"))
                        samplerate_before = get_samplerate_status().get("active_rate")
                        logger.info(
                            "PLAYBACK-RESUME-DIAG controlled restart START: source=local url=%s "
                            "saved_expected_rate=%s saved_position=%.2f stale=%s old_file=%s "
                            "old_paused=%s old_ended=%s active_rate_before=%s",
                            local_url, local_expected_rate, saved_position,
                            playback_stream_stale_after_measurement,
                            old_file, old_paused, old_ended, samplerate_before,
                        )
                        release_done = False
                        rate_prearm_done = False
                        loadfile_done = False
                        seek_done = False
                        sink_suspended = False
                        try:
                            _clear_playback_queue()
                            _reset_mpv_loop_state()
                            player_instance.stop_playback()
                            released = await _wait_for_pipewire_mpv_release()
                            release_done = True
                            logger.info(
                                "PLAYBACK-RESUME-DIAG release_done=%s", released,
                            )
                            # Force the correct rate before loadfile - use saved snapshot
                            prearm_rate, prearm_generation = await _prearm_known_local_samplerate(
                                saved_track_info,
                                "playback-restart-after-measurement",
                            )
                            rate_prearm_done = True
                            active_rate_after_prearm = get_samplerate_status().get("active_rate")
                            logger.info(
                                "PLAYBACK-RESUME-DIAG rate_prearm_done=True active_rate_after_prearm=%s",
                                active_rate_after_prearm,
                            )
                            # If prearm did not actually move the clock to expected_rate,
                            # do a sink suspend/resume to force the PipeWire clock.
                            # _prearm_known_local_samplerate only sets force_rate;
                            # it does not do the actual clock change.
                            if (
                                isinstance(active_rate_after_prearm, int)
                                and active_rate_after_prearm != local_expected_rate
                            ):
                                sink_suspended = await _suspend_resume_playback_sink(
                                    reason="playback-restart-after-measurement",
                                    force=True,
                                )
                                active_rate_after_sink = get_samplerate_status().get("active_rate")
                                logger.info(
                                    "PLAYBACK-RESUME-DIAG sink_suspend_resume suspended=%s active_rate_after_sink=%s",
                                    sink_suspended, active_rate_after_sink,
                                )
                                # Verify: sink suspend/resume must have actually moved the
                                # PipeWire clock to local_expected_rate. If not, calling
                                # loadfile would still play at 48 kHz and the result log
                                # would falsely look like a success. Keep the stale flag
                                # (do NOT clear it), surface a clear failure, and do NOT
                                # call loadfile.
                                if (
                                    not isinstance(active_rate_after_sink, int)
                                    or active_rate_after_sink != local_expected_rate
                                ):
                                    logger.error(
                                        "PLAYBACK-RESUME-DIAG controlled_restart_failed "
                                        "rate_still_mismatched_after_sink_suspend "
                                        "expected=%s active=%s source=local url=%s "
                                        "keeping_stale_flag=True",
                                        local_expected_rate, active_rate_after_sink, local_url,
                                    )
                                    # Stale flag intentionally NOT cleared here.
                                    raise HTTPException(
                                        status_code=500,
                                        detail=(
                                            f"Playback restart after measurement failed: "
                                            f"PipeWire clock did not move to expected "
                                            f"{local_expected_rate} Hz after sink suspend/resume "
                                            f"(active={active_rate_after_sink}). Stale stream "
                                            f"flag kept; playback not resumed."
                                        ),
                                    )
                            # loadfile
                            player_instance.loadfile(local_url, mode="replace")
                            loadfile_done = True
                            logger.info(
                                "PLAYBACK-RESUME-DIAG loadfile_done=True url=%s", local_url,
                            )
                            # Wait for the new file to actually be loaded before seeking
                            file_settled = await _wait_for_player_current_file(local_url, timeout_ms=1600)
                            if not file_settled:
                                logger.warning(
                                    "PLAYBACK-RESUME-DIAG file_settle_timeout url=%s proceeding_anyway",
                                    local_url,
                                )
                            # Seek back to saved position (best-effort, non-fatal)
                            if saved_position > 0.5:
                                try:
                                    player_instance.seek(saved_position)
                                    seek_done = True
                                    logger.info(
                                        "PLAYBACK-RESUME-DIAG seek_done=True seek_position=%.2f",
                                        saved_position,
                                    )
                                except Exception as seek_exc:
                                    logger.warning(
                                        "PLAYBACK-RESUME-DIAG seek_failed seek_position=%.2f error=%s",
                                        saved_position, seek_exc,
                                    )
                            player_instance.set_pause(False)
                            _mark_player_state_authoritative(player_instance.state)
                            if prearm_rate and prearm_generation:
                                asyncio.create_task(_release_local_samplerate_prearm(
                                    prearm_rate, prearm_generation, "playback-restart-after-measurement",
                                ))
                            new_state = player_instance.state
                            new_file = new_state.get("current_file", "")
                            samplerate_after = get_samplerate_status().get("active_rate")
                            # All steps succeeded: clear stale flag
                            playback_stream_stale_after_measurement = False
                            _playback_state_before_measurement = None
                            # Sync subwoofer helper at the restored playback rate.
                            # The measurement release watcher may have already synced at
                            # 48000 before this controlled restart changed the clock.
                            if subwoofer_runtime is not None:
                                await _sync_subwoofer_runtime_at_rate(local_expected_rate)
                            logger.info(
                                "PLAYBACK-RESUME-DIAG result=controlled_restart_after_measurement "
                                "source=local url=%s expected_rate=%s active_rate_before=%s active_rate_after=%s "
                                "new_file=%s seek_position=%.2f release_done=%s rate_prearm_done=%s "
                                "sink_suspended=%s loadfile_done=%s seek_done=%s loadfile_mode=replace",
                                local_url, local_expected_rate, samplerate_before, samplerate_after,
                                new_file, saved_position, release_done, rate_prearm_done,
                                sink_suspended, loadfile_done, seek_done,
                            )
                            return {
                                "status": "playing",
                                "playback": build_playback_payload(new_state),
                            }
                        except HTTPException:
                            # Re-raise our own 5xx (e.g. rate_still_mismatched_after_sink_suspend)
                            # so the detail is not wrapped in a generic message.
                            raise
                        except Exception as exc:
                            logger.error(
                                "PLAYBACK-RESUME-DIAG controlled_restart_failed keeping_stale_flag=True "
                                "source=local url=%s expected_rate=%s release_done=%s rate_prearm_done=%s "
                                "sink_suspended=%s loadfile_done=%s seek_done=%s error=%s",
                                local_url, local_expected_rate, release_done, rate_prearm_done,
                                sink_suspended, loadfile_done, seek_done, exc,
                            )
                            raise HTTPException(
                                status_code=500,
                                detail=f"Playback restart after measurement failed: {exc}",
                            )
                # Radio stream is stale after measurement at 48 kHz.
                # Instead of unpausing the old stream, do a controlled restart
                # at the radio stream's native sample rate.
                if source == "radio" and radio_stream_stale_after_measurement:
                    radio_url = (_radio_state_before_measurement or {}).get("url") or (current_track_info or {}).get("url")
                    expected_rate = (_radio_state_before_measurement or {}).get("expected_rate") or 44100
                    if radio_url:
                        # Snapshot old state for diagnostics
                        old_state = player_instance.state
                        old_file = old_state.get("current_file", "")
                        old_paused = bool(old_state.get("paused"))
                        old_ended = bool(old_state.get("ended"))
                        samplerate_before = get_samplerate_status().get("active_rate")
                        logger.info(
                            "RADIO-RESUME-DIAG controlled restart START: url=%s expected_rate=%s "
                            "stale=%s old_file=%s old_paused=%s old_ended=%s active_rate_before=%s",
                            radio_url, expected_rate, radio_stream_stale_after_measurement,
                            old_file, old_paused, old_ended, samplerate_before,
                        )
                        # Stop old stream first, then wait for PipeWire release
                        _clear_playback_queue()
                        _reset_mpv_loop_state()
                        player_instance.stop_playback()
                        released = await _wait_for_pipewire_mpv_release()
                        logger.info(
                            "RADIO-RESUME-DIAG old stream release: released=%s",
                            released,
                        )
                        # Now set the correct rate and load fresh
                        await _ensure_radio_samplerate_force(expected_rate, "radio-restart-after-measurement")
                        player_instance.loadfile(radio_url, mode="replace")
                        player_instance.set_pause(False)
                        _mark_player_state_authoritative(player_instance.state)
                        # Verify the new stream
                        new_state = player_instance.state
                        new_file = new_state.get("current_file", "")
                        samplerate_after = get_samplerate_status().get("active_rate")
                        # Success: clear stale flag (both radio-specific and generic)
                        radio_stream_stale_after_measurement = False
                        _radio_state_before_measurement = None
                        playback_stream_stale_after_measurement = False
                        _playback_state_before_measurement = None
                        # Sync subwoofer helper at the restored playback rate
                        if subwoofer_runtime is not None:
                            await _sync_subwoofer_runtime_at_rate(expected_rate)
                        # Schedule recovery tasks
                        asyncio.create_task(_maybe_recover_samplerate_mismatch((current_track_info or {}).copy()))
                        state_seq = new_state.get("_seq") if isinstance(new_state, dict) else None
                        _schedule_silent_active_watch(
                            source="radio",
                            signature=f"player:radio:{radio_url}",
                            track=(current_track_info or {}).copy(),
                        )
                        logger.info(
                            "RADIO-RESUME-DIAG result=controlled_restart_after_measurement "
                            "url=%s expected_rate=%s active_rate_before=%s active_rate_after=%s new_file=%s loadfile=replace",
                            radio_url, expected_rate, samplerate_before, samplerate_after, new_file,
                        )
                        return {
                            "status": "playing",
                            "playback": build_playback_payload(new_state),
                        }
                    # No URL: clear stale flag to avoid blocking
                    radio_stream_stale_after_measurement = False
                    _radio_state_before_measurement = None
                    logger.warning(
                        "RADIO-RESUME-DIAG stale flag cleared without restart (no URL found)",
                    )
                # PATCH 3: safety rule - if was_paused and state.current_file
                # does NOT match current_track_info.url, do NOT blindly
                # set_pause(False) on the wrong file. Refuse with a clear 409
                # so the UI knows it must reload the track.
                if was_paused and state.get("current_file") and current_track_info:
                    state_file = state.get("current_file")
                    cur_url = (current_track_info or {}).get("url") or (current_track_info or {}).get("path")
                    if state_file and cur_url and state_file != cur_url:
                        logger.error(
                            "PLAYBACK-RESUME-DIAG refusing_resume_due_to_state_track_mismatch "
                            "state_file=%s current_track_info_url=%s source=%s reason=state_drift",
                            state_file, cur_url, (current_track_info or {}).get("source"),
                        )
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"Refusing to resume: mpv current_file does not match "
                                f"current_track_info (state='{state_file}', track='{cur_url}'). "
                                f"Reload the track from the UI."
                            ),
                        )
                prearm_rate, prearm_generation = await _prearm_known_local_samplerate(
                    current_track_info,
                    "toggle-resume",
                )
        player_instance.set_pause(False if was_paused else True)
        new_state = player_instance.state
        _mark_player_state_authoritative(new_state)
        if was_paused and prearm_rate and prearm_generation:
            asyncio.create_task(_release_local_samplerate_prearm(prearm_rate, prearm_generation, "toggle-resume"))
        if was_paused and current_track_info and (current_track_info.get("source") in {"local", "radio"}):
            asyncio.create_task(_maybe_recover_samplerate_mismatch((current_track_info or {}).copy()))
            state_seq = new_state.get("_seq") if isinstance(new_state, dict) else None
            _schedule_silent_active_watch(
                source=current_track_info.get("source"),
                signature=f"player:{current_track_info.get('source')}:{current_track_info.get('url')}",
                track=(current_track_info or {}).copy(),
            )
        return {
            "status": "paused" if new_state.get("paused") else "playing",
            "playback": build_playback_payload(new_state),
        }

    replay_track = current_track_info or last_radio_track_info
    replay_url = (replay_track or {}).get("url")
    if not replay_url:
        # No URL available: clear stale state and broadcast so UI is clean.
        current_track_info = None
        last_radio_track_info = None
        radio_reconnect_attempts = 0
        radio_reconnect_url = None
        radio_reconnect_active_since = 0.0
        _clear_playback_queue()
        if player_instance and player_instance._running:
            try:
                _reset_mpv_loop_state()
            except Exception:
                pass
            try:
                player_instance.stop_playback()
            except Exception:
                pass
            _mark_player_state_authoritative(player_instance.state)
        await manager.broadcast({"type": "playback", "data": build_playback_payload(player_instance.state if player_instance else {})})
        raise HTTPException(status_code=409, detail="Nothing is available to replay")

    await pause_spotify_for_local_playback_broadcast()
    await _wait_for_pipewire_mpv_release()
    prearm_rate, prearm_generation = await _prearm_known_local_samplerate(replay_track, "replay")
    if replay_track.get("source") == "radio":
        radio_reconnect_attempts = 0
        radio_reconnect_url = None
        radio_reconnect_active_since = 0.0
        _clear_playback_queue()
        _reset_mpv_loop_state()
    player_instance.loadfile(replay_url, mode="replace")
    current_track_info = dict(replay_track)
    last_track_info = dict(replay_track)
    if current_track_info.get("source") == "radio":
        last_radio_track_info = dict(current_track_info)
    _mark_player_state_authoritative(player_instance.state)
    if prearm_rate and prearm_generation:
        asyncio.create_task(_release_local_samplerate_prearm(prearm_rate, prearm_generation, "replay"))
    asyncio.create_task(_maybe_recover_samplerate_mismatch((replay_track or {}).copy()))
    if replay_track and replay_track.get("source") in {"local", "radio"}:
        state_seq = player_instance.state.get("_seq") if isinstance(player_instance.state, dict) else None
        _schedule_silent_active_watch(
            source=replay_track.get("source"),
            signature=f"player:{replay_track.get('source')}:{replay_url}",
            track=(replay_track or {}).copy(),
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
    if current_track_info and current_track_info.get("source") == "radio":
        last_radio_track_info = dict(current_track_info)
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
    vol = max(0, min(100, vol))
    try:
        applied_volume = set_output_volume(vol)
    except SystemVolumeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to set output volume: {exc}")
    ensure_local_source_volume()
    # Keep local/radio output-volume changes responsive. Spotify volume uses
    # /api/spotify/volume, so this endpoint should not block on multiple
    # playerctl/Spotify status reads on slow boards.
    await manager.broadcast({"type": "playback", "data": build_playback_payload(player_instance.state)})
    return {"volume": applied_volume}

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
    if playback_queue_mode == "mpv_native" and had_queue:
        _trim_mpv_native_queue_to_current()
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

    if not _set_queue_shuffle(enabled):
        raise HTTPException(status_code=409, detail="Shuffle requires an active local queue")

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
    return {"status": "ok", "position": pos, "playback": build_playback_payload(player_instance.state)}

@app.get("/api/status")
async def get_status():
    if player_instance:
        state = build_playback_payload(player_instance.state)
        state["metadata"] = player_instance.get_metadata() if state.get("current_file") else {}
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
    else:
        last_measurement_window_seen_at = time.monotonic()
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

async def _maybe_repair_active_app_samplerate_drift(status: dict) -> None:
    global last_app_samplerate_drift_repair_at
    now = time.monotonic()
    if now - last_app_samplerate_drift_repair_at < 2.0:
        return
    state = player_instance.state if player_instance and player_instance._running else {}
    if not _is_local_playback_active(state) or not _playback_state_matches_track(state, current_track_info):
        return
    source = (current_track_info or {}).get("source")
    if source not in {"local", "radio"}:
        return
    active_rate = status.get("active_rate") if isinstance(status, dict) else None
    expected_rate = await _resolve_expected_playback_samplerate(source)
    if not isinstance(expected_rate, int) or expected_rate <= 0 or active_rate == expected_rate:
        return
    last_app_samplerate_drift_repair_at = now
    logger.info(
        "Repairing active app samplerate drift from status poll: source=%s expected_rate=%s active_rate=%s track=%s",
        source,
        expected_rate,
        active_rate,
        (current_track_info or {}).get("url"),
    )
    await _ensure_radio_samplerate_force(expected_rate, f"status-drift-repair:{source}")


@app.get("/api/audio/samplerate")
async def audio_samplerate_status():
    status = get_samplerate_status()
    logger.info(
        "audio_samplerate_status entry: footer_owner=%s active_rate=%s sink_state=%s",
        current_footer_owner,
        status.get("active_rate"),
        (status.get("relevant_sink") or {}).get("state"),
    )
    try:
        await _maybe_repair_active_app_samplerate_drift(status)
    except Exception as exc:
        logger.warning("Active app samplerate drift repair failed: %s", exc)
    return get_samplerate_status()


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
        await _sync_subwoofer_runtime(result)
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

    try:
        result = set_audio_output_mode(mode, subwoofer, subwoofers)

        # Log pre-switch convolver state for diagnostics
        if easyeffects_manager is not None:
            compare_pre = easyeffects_manager.load_compare_state()
            ee_pre = easyeffects_manager.get_active_preset()
            logger.info(
                "CONVOLVER-SLOT pre-switch: new_mode=%s compare_pre=%s ee_active_pre=%s",
                mode,
                json.dumps(compare_pre, sort_keys=True, default=str) if compare_pre else '{}',
                ee_pre,
            )

        await _sync_subwoofer_runtime(result)

        # ── Convolver Slot Consistency After Mode Switch ──
        # When switching modes (especially 2.x → Stereo), the EE graph is
        # rebuilt but the loaded preset may drift from the compare state's
        # active slot. Re-apply the user's selected slot to keep UI ↔ EE in sync.
        if easyeffects_manager is not None:
            compare = easyeffects_manager.load_compare_state()
            active_side = compare.get("activeSide") if compare.get("activeSide") in {"A", "B"} else None
            if active_side == "A":
                side_preset = compare.get("presetA", "")
            elif active_side == "B":
                side_preset = compare.get("presetB", "")
            else:
                side_preset = ""
            ee_active = easyeffects_manager.get_active_preset()
            logger.info(
                "CONVOLVER-SLOT mode switch: new_mode=%s compare_activeSide=%s "
                "compare_presetA=%s compare_presetB=%s ee_active_preset=%s",
                mode, active_side, compare.get("presetA"), compare.get("presetB"), ee_active,
            )
            if side_preset and ee_active and ee_active != side_preset:
                logger.warning(
                    "CONVOLVER-SLOT MISMATCH: compare says %s=%s but EE has %s loaded. Re-applying %s.",
                    active_side, side_preset, ee_active, side_preset,
                )
                try:
                    easyeffects_manager.load_preset(side_preset)
                except Exception as exc:
                    logger.warning("CONVOLVER-SLOT re-apply failed: %s", exc)
            elif not side_preset:
                logger.info("CONVOLVER-SLOT no active compare slot, skipping re-apply (ee_active=%s)", ee_active)
            else:
                logger.info("CONVOLVER-SLOT consistent: compare=%s EE=%s", side_preset, ee_active)

        # Enrich 2.2 response with derived delays for API/debug verification
        om = result.get("output_mode") or {}
        if om.get("mode") in OUTPUT_MODE_SUBWOOFER_22_MODES:
            cfg = SubwooferRuntimeConfig.from_overview(result)
            om["derived_main_delay_ms"] = cfg.derived_main_delay_ms
            om["derived_sub1_delay_ms"] = cfg.derived_sub1_delay_ms
            om["derived_sub2_delay_ms"] = cfg.derived_sub2_delay_ms
            result["output_mode"] = om

        if subwoofer_runtime is not None:
            result["output_mode"] = {
                **(result.get("output_mode") or {}),
                "runtime": subwoofer_runtime.snapshot(),
            }
        await refresh_peak_monitor_after_effects_change("audio-output-mode-switch")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
    limiter_enabled = bool(body.get("limiterEnabled", body.get("limiter_enabled", False)))
    headroom_enabled = bool(body.get("headroomEnabled", body.get("headroom_enabled", False)))
    headroom_gain_db = float(body.get("headroomGainDb", body.get("headroom_gain_db", -3.0)) or -3.0)
    autogain_enabled = bool(body.get("autogainEnabled", body.get("autogain_enabled", False)))
    autogain_target_db = float(body.get("autogainTargetDb", body.get("autogain_target_db", -12.0)) or -12.0)
    delay_enabled = bool(body.get("delayEnabled", body.get("delay_enabled", False)))
    delay_left_ms = float(body.get("delayLeftMs", body.get("delay_left_ms", 0.0)) or 0.0)
    delay_right_ms = float(body.get("delayRightMs", body.get("delay_right_ms", 0.0)) or 0.0)
    bass_enabled = bool(body.get("bassEnabled", body.get("bass_enabled", False)))
    bass_amount = float(body.get("bassAmount", body.get("bass_amount", 0.0)) or 0.0)
    tone_effect_enabled = bool(body.get("toneEffectEnabled", body.get("tone_effect_enabled", False)))
    tone_effect_mode = str(body.get("toneEffectMode", body.get("tone_effect_mode", "crystalizer")) or "crystalizer").strip().lower()
    return {
        "limiter": {"enabled": limiter_enabled},
        "headroom": {
            "enabled": headroom_enabled,
            "params": {
                "gainDb": headroom_gain_db,
            },
        },
        "autogain": {
            "enabled": autogain_enabled,
            "params": {
                "targetDb": autogain_target_db,
            },
        },
        "delay": {
            "enabled": delay_enabled,
            "params": {
                "leftMs": delay_left_ms,
                "rightMs": delay_right_ms,
            },
        },
        "bass_enhancer": {
            "enabled": bass_enabled,
            "params": {
                "amount": bass_amount,
                "harmonics": 8.5,
                "scope": 100.0,
                "blend": 0.0,
            },
        },
        "tone_effect": {
            "enabled": tone_effect_enabled,
            "mode": tone_effect_mode,
        },
    }


def _resolve_effects_extras(extras: dict | None = None) -> dict:
    global easyeffects_manager
    if not easyeffects_manager:
        return extras or {}
    if extras is None:
        return easyeffects_manager.load_global_extras()
    return easyeffects_manager.normalize_effects_extras(extras)


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

    extras = _resolve_effects_extras(_parse_effects_extras_from_json(body))
    result = ee_manager.apply_global_extras_to_all_presets(extras)

    active_preset = ee_manager.get_active_preset()
    if active_preset and active_preset not in ee_manager.EXCLUDED_GLOBAL_EXTRAS_PRESETS:
        try:
            ee_manager.load_preset(active_preset)
        except Exception as e:
            logger.warning("Failed to reload active preset after extras update: %s", e)

    status = ee_manager.get_status()
    await manager.broadcast({"type": "easyeffects", "data": status})
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

def _normalize_measurement_optional_input_channel(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        channel = int(str(value).strip())
    except (TypeError, ValueError):
        return ""
    return str(channel) if channel >= 1 else ""


def _measurement_setup_settings_from_payload(settings: dict[str, Any]) -> dict[str, Any]:
    measure_settings = settings.get("measure") if isinstance(settings.get("measure"), dict) else {}
    reference_input_channel = measure_settings.get("selectedReferenceInputChannel")
    if reference_input_channel is None:
        reference_input_channel = measure_settings.get("reference_input_channel")
    return {
        "selectedReferenceInputChannel": _normalize_measurement_optional_input_channel(reference_input_channel),
    }


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

    restore_force_rate = None
    measurement_rate = _resolve_measurement_start_sample_rate()
    try:
        _capture_playback_state_before_measurement()
        restore_force_rate = await _prepare_subwoofer_runtime_for_measurement_start(measurement_rate)
        job = await measurement_store.start_measurement(
            input_id=input_id,
            channel=channel,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            calibration_ref=calibration_ref,
        )
        if restore_force_rate is not None:
            asyncio.create_task(_release_measurement_samplerate_force_after_job(job["id"], measurement_rate, restore_force_rate))
    except ValueError as exc:
        if restore_force_rate is not None:
            _set_pipewire_force_rate(restore_force_rate)
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        if restore_force_rate is not None:
            _set_pipewire_force_rate(restore_force_rate)
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

    restore_force_rate = None
    measurement_rate = _resolve_measurement_start_sample_rate()
    try:
        _capture_playback_state_before_measurement()
        restore_force_rate = await _prepare_subwoofer_runtime_for_measurement_start(measurement_rate)
        job = await measurement_store.start_lr_repeat_measurement(
            input_id=input_id,
            base_name=base_name,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            calibration_ref=calibration_ref,
        )
        if restore_force_rate is not None:
            asyncio.create_task(_release_measurement_samplerate_force_after_job(job["id"], measurement_rate, restore_force_rate))
    except ValueError as exc:
        if restore_force_rate is not None:
            _set_pipewire_force_rate(restore_force_rate)
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        if restore_force_rate is not None:
            _set_pipewire_force_rate(restore_force_rate)
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


def _auto_sub_cancel_requested(job: dict[str, Any]) -> bool:
    return bool(job.get("cancel_requested")) or str(job.get("status") or "").lower() == "cancelled"


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
) -> dict[str, Any]:
    sub1 = _auto_sub_22_sub(snapshot, "sub1")
    sub2 = _auto_sub_22_sub(snapshot, "sub2")
    sub1["alignment_ms"] = _auto_sub_clamped_delay(sub1_alignment_ms)
    sub2["alignment_ms"] = _auto_sub_clamped_delay(sub2_alignment_ms)
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


@app.post("/api/measurements/auto-sub-optimize/start")
async def start_auto_sub_optimize(
    input_id: str = Form(...),
    channel: str = Form("left"),
    mic_input_channel: str = Form("1"),
    reference_input_channel: str = Form(""),
    calibration_ref: str = Form(""),
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
    job["status"] = "cancelled"
    job["message"] = "Auto Sub Optimize cancelled."
    job["error"] = None

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

    restore_force_rate = None
    pre_arm_failed = False
    try:
        restore_force_rate = await _prepare_subwoofer_runtime_for_measurement_start(auto_sub_rate)
        _marks["pre_arm"] = time.monotonic()
        if _auto_sub_cancel_requested(job):
            if restore_force_rate is not None:
                _set_pipewire_force_rate(restore_force_rate)
            return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
    except Exception as exc:
        logger.exception("Auto-sub: pre-arm failed for delay %.2f ms", delay_ms)
        pre_arm_failed = True
        if restore_force_rate is not None:
            _set_pipewire_force_rate(restore_force_rate)
        return _return_candidate({
            "delay_ms": delay_ms,
            "name": str(delay_ms),
            "points": [],
            "sweep_id": "",
            "status": "pre_arm_failed",
            "error": str(exc),
            "scan": stage,
        })
    sweep_id = ""
    try:
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
        _marks["release_start"] = time.monotonic()
        if restore_force_rate is not None:
            try:
                await _release_measurement_samplerate_force_after_job(sweep_id, auto_sub_rate, restore_force_rate)
            except Exception as exc:
                logger.warning("Auto-sub: samplerate release failed for sweep %s: %s", sweep_id, exc)
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
            return _return_candidate({
                "delay_ms": delay_ms,
                "name": str(delay_ms),
                "points": points,
                "sweep_id": sweep_id,
                "status": "completed",
                "scan": stage,
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
        })
    except Exception as exc:
        logger.exception("Auto-sub: sweep error for delay %.2f ms", delay_ms)
        if job.get("current_sweep_id") == sweep_id:
            job["current_sweep_id"] = ""
        if restore_force_rate is not None:
            try:
                _set_pipewire_force_rate(restore_force_rate)
            except Exception:
                pass
        return _return_candidate({
            "delay_ms": delay_ms,
            "name": str(delay_ms),
            "points": [],
            "sweep_id": sweep_id,
            "status": "error",
            "error": str(exc),
            "scan": stage,
        })


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
        "sweep_id": left_result.get("sweep_id", ""),
        "sweep_id_left": left_result.get("sweep_id", ""),
        "sweep_id_right": right_result.get("sweep_id", ""),
        "status": status,
        "scan": stage,
        "status_left": left_result.get("status"),
        "status_right": right_result.get("status"),
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
        })
    return candidate


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

    try:
        if _auto_sub_cancel_requested(job):
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
        best_sub1 = _auto_sub_clamped_delay(float(winner.get("sub1_alignment_ms", sub1_winner_delay) or sub1_winner_delay))
        best_sub2 = _auto_sub_clamped_delay(float(winner.get("sub2_alignment_ms", sub2_winner_delay) or sub2_winner_delay))

        apply_ok = False
        try:
            sub_config = _auto_sub_22_global_config(original_config_snapshot)
            subwoofers_config = _auto_sub_22_candidate_subwoofers(
                original_config_snapshot,
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
            "sweep_count": matrix_sweep_total,
            "candidate_count": len(sub1_scan_delays) + len(sub2_scan_delays) + len(matrix_pairs),
            "sub1_coarse_candidate_count": len(sub1_scan_delays),
            "sub2_coarse_candidate_count": len(sub2_scan_delays),
            "matrix_candidate_count": len(matrix_pairs),
            "valid_count": len(matrix_valid),
            "sub1_coarse_valid_count": len(coarse1_valid),
            "sub2_coarse_valid_count": len(coarse2_valid),
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

    try:
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
            "sweep_count": actual_sweep_total,
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

    if _auto_sub_cancel_requested(job):
        job["message"] = "Auto Sub Optimize cancelled."
        await _restore_original_config()
        return

    try:
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
        stored_fine_accepted = bool(acceptance["fine_accepted"] and auto_apply)
        stored_reject_reason = acceptance["reject_reason"]
        if not auto_apply and stored_winner is incumbent_winner and round(float(best_delay), 2) != round(float(current_alignment), 2):
            stored_reject_reason = apply_decision
        fine_scan["accepted_winner"] = stored_winner
        fine_scan["fine_accepted"] = stored_fine_accepted
        fine_scan["reject_reason"] = stored_reject_reason

        job["status"] = "completed"
        job["message"] = (
            f"Applied: {best_delay} ms (score {winner['score_pct']:.0f} %)"
            if auto_apply
            else f"Suggested: {best_delay} ms (not applied: {confidence})"
        )
        _log_auto_sub_timing_summary(job)
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
            "sweep_count": total,
            "candidate_count": coarse_total + len(fine_delays),
            "coarse_candidate_count": coarse_total,
            "fine_candidate_count": len(fine_delays),
            "coarse_sweep_count": coarse_sweep_total,
            "fine_sweep_count": len(fine_delays) * 2,
            "valid_count": len(combined_valid),
            "coarse_valid_count": len(valid),
            "fine_valid_count": len(fine_valid),
            "fine_scan": fine_scan,
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
            await subwoofer_runtime.reclean_direct_easyeffects_links()
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
    global source_transition_lock
    if source_transition_lock is None:
        source_transition_lock = asyncio.Lock()
    async with source_transition_lock:
        data = await _complete_spotify_entry_handoff()
        return await broadcast_spotify_state(data)


@app.post("/api/spotify/pause")
async def api_spotify_pause():
    data = await spotify_pause()
    return await broadcast_spotify_state(data)


@app.post("/api/spotify/toggle")
async def api_spotify_toggle():
    global source_transition_lock
    if source_transition_lock is None:
        source_transition_lock = asyncio.Lock()
    async with source_transition_lock:
        sd = await get_spotify_ui_state()
        if sd.get("status") != "Playing":
            data = await _complete_spotify_entry_handoff()
        else:
            data = await spotify_toggle()
        return await broadcast_spotify_state(data)


@app.post("/api/spotify/next")
async def api_spotify_next():
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
        applied_volume = set_output_volume(volume)
    except SystemVolumeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to set output volume: {exc}")
    ensure_local_source_volume()
    data = await broadcast_spotify_state()
    data["volume"] = applied_volume
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
