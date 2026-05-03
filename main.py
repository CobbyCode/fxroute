# SPDX-License-Identifier: AGPL-3.0-only

"""Main FastAPI application for FXRoute."""

import json
import logging
import re
import shutil
import time
import asyncio
import random
import subprocess
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from config import get_settings

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

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
        if (entry.get("properties") or {}).get("application.name") == "spotify"
        or (entry.get("properties") or {}).get("application.id") == "spotify"
        or (entry.get("properties") or {}).get("node.name") == "spotify"
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
        data = await spotify_play()
        if data.get("status") != "Playing":
            return False, None, None
        return await _wait_for_pipewire_spotify_samplerate_alignment()


async def _complete_spotify_entry_handoff() -> dict:
    global spotify_samplerate_recovery_active
    await pause_local_playback_for_spotify_broadcast()
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
    global local_samplerate_prearm_generation
    try:
        aligned = await _wait_for_samplerate_alignment(expected_rate, timeout_ms=1200)
        if generation != local_samplerate_prearm_generation:
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
    global local_samplerate_prearm_generation
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


def _is_local_playback_active(state: dict | None) -> bool:
    state = state or {}
    return bool(state.get("current_file") and not state.get("paused") and not state.get("ended"))


def _is_spotify_playback_active(state: dict | None) -> bool:
    state = state or {}
    return bool(state.get("available") and state.get("status") == "Playing")


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
    DeleteTracksRequest,
    DownloadTracksRequest,
    PlaylistSaveRequest,
    PlayRequest,
    StationUpsertRequest,
)
from player import get_player, MPVNotInstalledError, MPVError
from stations import add_station, delete_station, get_stations, update_station
from playlists import delete_playlist, get_playlists, save_playlist
from library import LibraryScanner
from downloader import Downloader
from easyeffects import EasyEffectsManager
from measurement import MeasurementStore
from peak_monitor import EasyEffectsPeakMonitor
from samplerate import (
    SOURCE_MODE_APP_PLAYBACK,
    SOURCE_MODE_BLUETOOTH_INPUT,
    SOURCE_MODE_EXTERNAL_INPUT,
    apply_persisted_audio_output_selection,
    disconnect_connected_bluetooth_audio_sources,
    get_audio_output_overview,
    get_audio_source_overview,
    get_bluetooth_audio_overview,
    get_samplerate_status,
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
spotify_playerctl_last_trigger_at = 0.0
spotify_samplerate_recovery_lock = None
spotify_samplerate_recovery_active = False
local_samplerate_prearm_generation = 0
current_source_mode = SOURCE_MODE_APP_PLAYBACK
latest_spotify_state = None
current_footer_owner = "local"
last_spotify_samplerate_recovery_at = 0.0
current_track_info = None
last_track_info = None
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
    return {
        "audio_files": audio_files,
        "extracted_files": extracted_files,
        "skipped_entries": skipped_entries,
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
        await sync_peak_monitor_for_playback_state(player_instance.state)


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
        easyeffects_manager.load_preset(active_preset)
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
    if not isinstance(sink_rate, int) or sink_rate <= 0 or sink_rate == mpv_rate:
        return

    try:
        await _bounce_easyeffects_preset_for_samplerate_recovery(
            source_label="Local",
            expected_rate=mpv_rate,
            sink_rate=sink_rate,
            detail=f"track={expected_track.get('url')} source={expected_track.get('source')}",
            still_valid=lambda: _current_track_matches(expected_track),
        )
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
        player_instance.set_pause(False)
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
        if playback_queue_loop:
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
        prefix = playback_queue[: current_index + 1]
        remaining = playback_queue[current_index + 1 :]
        random.shuffle(remaining)
        playback_queue = prefix + remaining
        if playback_queue_mode == "mpv_native" and player_instance and player_instance._running:
            _prime_mpv_native_queue(current_index)
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
    return status


def build_playback_payload(state: Optional[dict] = None) -> dict:
    global current_track_info, easyeffects_manager, player_instance, peak_monitor
    playback_state = dict(state or (player_instance.state if player_instance else {}))
    source_volume = playback_state.get("volume") if isinstance(playback_state.get("volume"), (int, float)) else None
    if current_track_info and current_track_info.get("source") in {"local", "radio"}:
        playback_state["source_volume"] = int(round(float(source_volume))) if source_volume is not None else None
    elif source_volume is not None:
        playback_state["source_volume"] = int(round(float(source_volume)))
    playback_state["volume"] = get_output_volume_safe(int(round(float(source_volume))) if source_volume is not None else 100)
    playback_state["current_track"] = current_track_info
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
        if is_active_playback and not _playback_state_matches_track(state, current_track_info):
            logger.info(
                "Skipping peak monitor sync during unsettled player transition: source=%s state_file=%s track_url=%s track_id=%s",
                source,
                state.get("current_file"),
                (current_track_info or {}).get("url"),
                (current_track_info or {}).get("id"),
            )
            return
        desired_signature = f"player:{source}:{state.get('current_file') or ''}" if is_active_playback else None

        if is_active_playback and (not peak_monitor_playback_armed or peak_monitor_context_signature != desired_signature):
            peak_monitor_playback_armed = True
            peak_monitor_context_signature = desired_signature
            expected_rate = await _resolve_expected_playback_samplerate(source) if source in {"local", "radio"} else None
            aligned = await _wait_for_samplerate_alignment(expected_rate) if expected_rate else False
            if not aligned:
                await asyncio.sleep(PEAK_MONITOR_RESTART_SETTLE_MS / 1000)
            logger.info(
                "Restarting peak monitor on playback context change to refresh PipeWire links: %s (expected_rate=%s aligned=%s)",
                desired_signature,
                expected_rate,
                aligned,
            )
            await peak_monitor.restart()
            await manager.broadcast({"type": "playback_peak_warning", "data": peak_monitor.snapshot()})
        elif not is_active_playback and peak_monitor_playback_armed:
            await asyncio.sleep(PEAK_MONITOR_INACTIVE_GRACE_MS / 1000)
            refreshed_player_state = player_instance.state if player_instance else {}
            if refreshed_player_state.get("current_file") and not refreshed_player_state.get("paused") and not refreshed_player_state.get("ended"):
                return
            spotify_state = await get_spotify_ui_state()
            if spotify_state.get("available") and spotify_state.get("status") == "Playing":
                return
            logger.info("Stopping peak monitor while playback is inactive")
            await peak_monitor.stop()
            peak_monitor_playback_armed = False
            peak_monitor_context_signature = None
            await manager.broadcast({"type": "playback_peak_warning", "data": peak_monitor.snapshot()})


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
        elif not is_spotify_playing and peak_monitor_playback_armed:
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


# Callback functions
async def on_player_state_change(state: dict):
    global queue_advancing, playback_queue_index, current_track_info, last_track_info, queue_transition_target_url

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
                return
        finally:
            queue_advancing = False

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
    await manager.broadcast({"type": "spotify", "data": data})
    return data


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
    global settings, player_instance, library_scanner, downloader, easyeffects_manager, measurement_store, peak_monitor, peak_monitor_playback_armed, peak_monitor_transition_lock, peak_monitor_context_signature, easyeffects_preset_load_lock, source_transition_lock, external_input_loopback_module_id, external_input_loopback_source_name, bluetooth_input_source_name, bluetooth_monitor_task, bluetooth_agent_process, spotify_playerctl_watch_task, spotify_playerctl_detect_task, spotify_playerctl_last_trigger_at, spotify_samplerate_recovery_lock, spotify_samplerate_recovery_active, current_source_mode, latest_spotify_state

    # Startup
    logger.info("Starting FXRoute...")
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

        # Initialize library scanner
        library_scanner = LibraryScanner()
        library_scanner.refresh()
        logger.info("Library scanner initialized")

        # Initialize downloader
        downloader = Downloader()
        logger.info("Downloader initialized")

        # Initialize EasyEffects manager
        easyeffects_manager = EasyEffectsManager()
        logger.info("EasyEffects manager initialized")

        measurement_store = MeasurementStore()
        logger.info("Measurement store initialized: %s", measurement_store.measurements_dir)

        peak_monitor = EasyEffectsPeakMonitor(on_change=on_peak_monitor_change)
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

        # Register callbacks for state changes
        player_instance.register_callbacks(on_player_state_change)
        downloader.register_callback(on_download_progress, asyncio.get_running_loop())

        logger.info("Application startup complete")
    except Exception as e:
        logger.error(f"Startup failed: {e}")

    yield

    # Shutdown
    if player_instance:
        player_instance.stop()
        logger.info("MPV player stopped")
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
    await _disable_bluetooth_input_monitoring()
    try:
        set_bluetooth_receiver_enabled(False)
    except Exception:
        pass
    await _disable_external_input_loopback()
    if peak_monitor:
        await peak_monitor.stop()
        logger.info("EasyEffects output peak monitor stopped")

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

@app.get("/api/stations")
async def list_stations():
    stations = get_stations()
    return [
        {
            "id": s.id,
            "title": s.name,
            "image": s.custom_image_url or s.image_url or "",
            "image_url": s.image_url or "",
            "custom_image_url": s.custom_image_url or "",
            "stream_url": s.stream_url,
            "input_url": s.input_url or s.stream_url,
            "artist": "Radio",
        }
        for s in stations
    ]


@app.post("/api/stations")
async def create_station(req: StationUpsertRequest):
    try:
        station = add_station(req.name, req.stream_url, req.custom_image_url)
        return {
            "status": "ok",
            "station": {
                "id": station.id,
                "title": station.name,
                "image": station.custom_image_url or station.image_url or "",
                "image_url": station.image_url or "",
                "custom_image_url": station.custom_image_url or "",
                "stream_url": station.stream_url,
                "input_url": station.input_url or station.stream_url,
                "artist": "Radio",
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/stations/{station_id}")
async def edit_station(station_id: str, req: StationUpsertRequest):
    try:
        station = update_station(station_id, req.name, req.stream_url, req.custom_image_url)
        return {
            "status": "ok",
            "station": {
                "id": station.id,
                "title": station.name,
                "image": station.custom_image_url or station.image_url or "",
                "image_url": station.image_url or "",
                "custom_image_url": station.custom_image_url or "",
                "stream_url": station.stream_url,
                "input_url": station.input_url or station.stream_url,
                "artist": "Radio",
            },
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
    if suffix not in UPLOAD_AUDIO_EXTENSIONS and suffix != ".zip":
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
                if not audio_files:
                    shutil.rmtree(album_dir, ignore_errors=True)
                    raise HTTPException(status_code=400, detail="ZIP contains no supported audio files")
            except Exception:
                shutil.rmtree(album_dir, ignore_errors=True)
                raise
            finally:
                temp_zip_path.unlink(missing_ok=True)

            tracks = library_scanner.refresh(force=True)
            return {
                "status": "imported",
                "kind": "zip",
                "filename": filename,
                "folder": album_dir.name,
                "path": str(album_dir),
                "track_count": len(tracks),
                "imported_track_count": len(audio_files),
                "skipped_entry_count": len(extraction["skipped_entries"]),
                "message": f"Imported {len(audio_files)} track{'s' if len(audio_files) != 1 else ''} from {filename}",
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
    music_root = settings.MUSIC_ROOT.resolve()

    for track_id in req.track_ids:
        track = tracks_by_id.get(track_id)
        if not track or not track.path:
            errors.append({"track_id": track_id, "error": "Track not found"})
            continue

        try:
            path = track.path.resolve()
            if music_root not in path.parents and path != music_root:
                errors.append({"track_id": track_id, "error": "Track path outside music root"})
                continue
            path.unlink()
            deleted.append(track_id)
        except Exception as e:
            errors.append({"track_id": track_id, "error": str(e)})

    tracks = library_scanner.refresh(force=True)
    return {
        "status": "ok",
        "deleted": deleted,
        "errors": errors,
        "track_count": len(tracks),
    }

@app.post("/api/play")
async def play_track(req: PlayRequest):
    source = req.source
    track_id = req.track_id
    url = req.url
    queue_track_ids = req.queue_track_ids or []
    global player_instance, current_track_info, last_track_info, source_transition_lock, current_footer_owner
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
    try:
        async with source_transition_lock:
            # Source exclusivity: pause Spotify and broadcast the new Spotify state
            current_footer_owner = "local"
            await pause_spotify_for_local_playback_broadcast()
            play_url = url
            track_info = {"id": track_id, "title": track_id, "artist": "", "source": source, "url": play_url}

            if source == "radio":
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
                    if not _prime_mpv_native_queue(playback_queue_index):
                        raise HTTPException(status_code=500, detail="Failed to initialize native mpv playlist")
                    if prearm_rate and prearm_generation:
                        asyncio.create_task(_release_local_samplerate_prearm(prearm_rate, prearm_generation, "play:mpv-native-queue"))
                else:
                    prearm_rate, prearm_generation = await _prearm_known_local_samplerate(track_info, "play")
                    player_instance.loadfile(play_url, mode="replace")
                    if prearm_rate and prearm_generation:
                        asyncio.create_task(_release_local_samplerate_prearm(prearm_rate, prearm_generation, "play"))
                    # Ensure MPV is unpaused after loadfile (it may stay paused if previously paused by Spotify)
                    player_instance.set_pause(False)

            current_track_info = track_info
            last_track_info = track_info

            if source in {"local", "radio"}:
                asyncio.create_task(_sync_peak_monitor_after_playback_transition(track_info.copy()))
                asyncio.create_task(_maybe_recover_samplerate_mismatch(track_info.copy()))

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
    return {
        "status": "paused" if new_state.get("paused") else "playing",
        "playback": build_playback_payload(new_state),
    }

@app.post("/api/playback/toggle")
async def toggle_playback():
    global player_instance, current_track_info
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    if not _can_send_play_command():
        state = player_instance.state
        return {"status": "paused" if state.get("paused") else "playing", "playback": build_playback_payload(state)}

    state = player_instance.state
    if state.get("current_file") and not state.get("ended"):
        player_instance.pause()
        new_state = player_instance.state
        return {
            "status": "paused" if new_state.get("paused") else "playing",
            "playback": build_playback_payload(new_state),
        }

    replay_track = current_track_info
    replay_url = (replay_track or {}).get("url")
    if not replay_url:
        raise HTTPException(status_code=409, detail="Nothing is available to replay")

    await pause_spotify_for_local_playback_broadcast()
    await _wait_for_pipewire_mpv_release()
    prearm_rate, prearm_generation = await _prearm_known_local_samplerate(replay_track, "replay")
    player_instance.loadfile(replay_url, mode="replace")
    if prearm_rate and prearm_generation:
        asyncio.create_task(_release_local_samplerate_prearm(prearm_rate, prearm_generation, "replay"))
    asyncio.create_task(_maybe_recover_samplerate_mismatch((replay_track or {}).copy()))
    return {
        "status": "playing",
        "replayed": True,
        "playback": build_playback_payload(player_instance.state),
    }

@app.post("/api/stop")
async def stop_playback():
    global player_instance, current_track_info
    if not player_instance or not player_instance._running:
        raise HTTPException(status_code=503, detail="Player not available")
    current_track_info = None
    _clear_playback_queue()
    _reset_mpv_loop_state()
    player_instance.stop_playback()
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
    await manager.broadcast({"type": "playback", "data": build_playback_payload(player_instance.state)})
    await broadcast_spotify_state()
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
        return state
    return {"running": False}

@app.get("/api/audio/samplerate")
async def audio_samplerate_status():
    status = get_samplerate_status()
    logger.info(
        "audio_samplerate_status entry: footer_owner=%s active_rate=%s sink_state=%s",
        current_footer_owner,
        status.get("active_rate"),
        (status.get("relevant_sink") or {}).get("state"),
    )
    return status


@app.get("/api/audio/outputs")
async def audio_output_overview():
    return get_audio_output_overview()


@app.post("/api/audio/outputs")
async def save_audio_output_selection_route(request: Request):
    try:
        body = await request.json()
        output_key = str(body.get("key", "")).strip()
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid JSON body, expected {"key": <string>}')

    try:
        result = set_audio_output_selection(output_key)
        await refresh_peak_monitor_after_effects_change("audio-output-switch")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to switch audio output: {exc}")


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

@app.get("/api/easyeffects/extras")
async def get_easyeffects_extras():
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")
    return {
        "status": "ok",
        "extras": easyeffects_manager.load_global_extras(),
        "excluded_presets": sorted(easyeffects_manager.EXCLUDED_GLOBAL_EXTRAS_PRESETS),
    }

@app.post("/api/easyeffects/extras")
async def save_easyeffects_extras(request: Request):
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    extras = _resolve_effects_extras(_parse_effects_extras_from_json(body))
    result = easyeffects_manager.apply_global_extras_to_all_presets(extras)

    active_preset = easyeffects_manager.get_active_preset()
    if active_preset and active_preset not in easyeffects_manager.EXCLUDED_GLOBAL_EXTRAS_PRESETS:
        try:
            easyeffects_manager.load_preset(active_preset)
        except Exception as e:
            logger.warning("Failed to reload active preset after extras update: %s", e)

    status = easyeffects_manager.get_status()
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
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")
    return easyeffects_manager.get_status()


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
    return FileResponse(preset_path, filename=preset_path.name)

@app.get("/api/measurements")
async def list_measurements():
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    return measurement_store.list_measurements()

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

@app.get("/api/browser-mic/certificate")
async def download_browser_mic_certificate():
    cert_path = Path("/etc/fxroute/certs/fxroute-local-root.crt")
    if not cert_path.exists():
        raise HTTPException(status_code=404, detail="Browser microphone certificate not available on this host")
    return FileResponse(cert_path, filename="fxroute-local-root.crt", media_type="application/x-x509-ca-cert")

@app.post("/api/measurements/start")
async def start_measurement(
    input_id: str = Form(...),
    channel: str = Form("left"),
    calibration_ref: str = Form(""),
    calibration_file: Optional[UploadFile] = File(None),
):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")

    calibration_bytes = None
    calibration_filename = None
    if calibration_file is not None:
        calibration_filename = calibration_file.filename or "calibration.txt"
        calibration_bytes = await calibration_file.read()

    try:
        job = await measurement_store.start_measurement(
            input_id=input_id,
            channel=channel,
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            calibration_ref=calibration_ref,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
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

@app.post("/api/measurements/browser/start")
async def start_browser_measurement(
    channel: str = Form("left"),
    calibration_ref: str = Form(""),
    calibration_file: Optional[UploadFile] = File(None),
):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")

    calibration_bytes = None
    calibration_filename = None
    if calibration_file is not None:
        calibration_filename = calibration_file.filename or "calibration.txt"
        calibration_bytes = await calibration_file.read()

    try:
        job = await measurement_store.start_browser_measurement(
            channel=channel,
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            calibration_ref=calibration_ref,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "ok", "job": job}

@app.post("/api/measurements/browser/complete")
async def complete_browser_measurement(
    job_id: str = Form(...),
    browser_input_label: str = Form("Browser microphone"),
    browser_capture_meta: str = Form(""),
    capture_file: UploadFile = File(...),
):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")

    capture_filename = capture_file.filename or "browser-capture.wav"
    capture_bytes = await capture_file.read()
    capture_meta = None
    if browser_capture_meta.strip():
        try:
            capture_meta = json.loads(browser_capture_meta)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid browser capture metadata JSON")

    try:
        job = await measurement_store.complete_browser_measurement(
            job_id=job_id,
            capture_filename=capture_filename,
            capture_bytes=capture_bytes,
            browser_input_label=browser_input_label,
            browser_capture_meta=capture_meta,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Measurement job not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "ok", "job": job}

@app.post("/api/measurements/save")
async def save_measurement(request: Request):
    global measurement_store
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    try:
        saved = measurement_store.save_measurement(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "measurement": saved}

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

@app.post("/api/easyeffects/compare")
async def save_easyeffects_compare(request: Request):
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    compare = easyeffects_manager.save_compare_state({
        "presetA": body.get("presetA", body.get("preset_a", "")),
        "presetB": body.get("presetB", body.get("preset_b", "")),
        "activeSide": body.get("activeSide", body.get("active_side")),
    })

    status = easyeffects_manager.get_status()
    await manager.broadcast({"type": "easyeffects", "data": status})
    return {"status": "ok", "compare": compare}

@app.post("/api/easyeffects/presets/combine")
async def combine_easyeffects_presets(request: Request):
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")

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
        created = easyeffects_manager.combine_presets(preset_name, preset_names)
        if load_after_create:
            easyeffects_manager.load_preset(created["name"])
        status = easyeffects_manager.get_status()
        await manager.broadcast({"type": "easyeffects", "data": status})
        if load_after_create:
            schedule_peak_monitor_refresh_after_effects_change("combine-presets")
        return {
            "status": "ok",
            "preset": created,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/easyeffects/presets/load")
async def load_easyeffects_preset(request: Request):
    global easyeffects_manager, easyeffects_preset_load_lock
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")

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
            easyeffects_manager.load_preset(preset_name)
            compare = easyeffects_manager.load_compare_state()
            if compare.get("presetA") == preset_name:
                compare["activeSide"] = "A"
                easyeffects_manager.save_compare_state(compare)
            elif compare.get("presetB") == preset_name:
                compare["activeSide"] = "B"
                easyeffects_manager.save_compare_state(compare)
            status = easyeffects_manager.get_status()
        await manager.broadcast({"type": "easyeffects", "data": status})
        schedule_peak_monitor_refresh_after_effects_change("preset-load")
        return {"status": "ok", "active_preset": preset_name, "compare": status.get("compare")}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/easyeffects/irs/upload")
async def upload_easyeffects_ir(file: UploadFile = File(...)):
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")

    tmp_path = None
    try:
        suffix = Path(file.filename or "upload.ir").suffix
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)

        uploaded = easyeffects_manager.upload_ir(tmp_path, file.filename or tmp_path.name)
        status = easyeffects_manager.get_status()
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
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")

    extras = _resolve_effects_extras({
        "limiter": {"enabled": limiter_enabled},
        "headroom": {"enabled": headroom_enabled, "params": {"gainDb": headroom_gain_db}},
        "autogain": {"enabled": autogain_enabled, "params": {"targetDb": autogain_target_db}},
        "delay": {
            "enabled": delay_enabled,
            "params": {"leftMs": delay_left_ms, "rightMs": delay_right_ms},
        },
        "tone_effect": {"enabled": tone_effect_enabled, "mode": tone_effect_mode},
    })

    try:
        created = easyeffects_manager.create_convolver_preset(preset_name, ir_filename, extras=extras)
        if load_after_create:
            easyeffects_manager.load_preset(created["name"])
        status = easyeffects_manager.get_status()
        await manager.broadcast({"type": "easyeffects", "data": status})
        schedule_peak_monitor_refresh_after_effects_change("create-convolver")
        return {
            "status": "ok",
            "preset": created,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/easyeffects/presets/import-json")
async def import_easyeffects_preset_json(
    file: UploadFile = File(...),
    load_after_create: bool = Form(False),
):
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")

    try:
        content = (await file.read()).decode("utf-8-sig")
        created = easyeffects_manager.import_preset_json(file.filename or "preset.json", content)
        if load_after_create:
            easyeffects_manager.load_preset(created["name"])
        status = easyeffects_manager.get_status()
        await manager.broadcast({"type": "easyeffects", "data": status})
        schedule_peak_monitor_refresh_after_effects_change("import-preset-json")
        return {
            "status": "ok",
            "preset": created,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Preset JSON is not valid UTF-8 text: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

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
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")

    extras = _resolve_effects_extras({
        "limiter": {"enabled": limiter_enabled},
        "headroom": {"enabled": headroom_enabled, "params": {"gainDb": headroom_gain_db}},
        "autogain": {"enabled": autogain_enabled, "params": {"targetDb": autogain_target_db}},
        "delay": {
            "enabled": delay_enabled,
            "params": {"leftMs": delay_left_ms, "rightMs": delay_right_ms},
        },
        "bass_enhancer": {
            "enabled": bass_enabled,
            "params": {"amount": bass_amount},
        },
        "tone_effect": {"enabled": tone_effect_enabled, "mode": tone_effect_mode},
    })

    tmp_path = None
    try:
        suffix = Path(file.filename or "upload.ir").suffix
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)

        created = easyeffects_manager.create_convolver_preset_with_upload(
            preset_name,
            tmp_path,
            file.filename or tmp_path.name,
            extras=extras,
        )
        if load_after_create:
            easyeffects_manager.load_preset(created["preset"]["name"])
        status = easyeffects_manager.get_status()
        await manager.broadcast({"type": "easyeffects", "data": status})
        schedule_peak_monitor_refresh_after_effects_change("create-with-ir")
        return {
            "status": "ok",
            "ir": created["ir"],
            "preset": created["preset"],
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"EasyEffects create-with-ir failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

@app.post("/api/easyeffects/presets/create-peq")
async def create_peq_preset(request: Request):
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")

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
        created = easyeffects_manager.create_peq_preset(preset_name, peq_definition, extras=extras)
        if load_after_create:
            easyeffects_manager.load_preset(created["name"])
        status = easyeffects_manager.get_status()
        await manager.broadcast({"type": "easyeffects", "data": status})
        schedule_peak_monitor_refresh_after_effects_change("create-peq")
        return {
            "status": "ok",
            "preset": created,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

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
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")

    try:
        content = await file.read()
        rew_text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="REW import file must be UTF-8 text")

    if not preset_name.strip():
        raise HTTPException(status_code=400, detail="preset_name is required")

    extras = _resolve_effects_extras({
        "limiter": {"enabled": limiter_enabled},
        "headroom": {"enabled": headroom_enabled, "params": {"gainDb": headroom_gain_db}},
        "autogain": {"enabled": autogain_enabled, "params": {"targetDb": autogain_target_db}},
        "delay": {
            "enabled": delay_enabled,
            "params": {"leftMs": delay_left_ms, "rightMs": delay_right_ms},
        },
        "bass_enhancer": {
            "enabled": bass_enabled,
            "params": {"amount": bass_amount},
        },
        "tone_effect": {"enabled": tone_effect_enabled, "mode": tone_effect_mode},
    })

    try:
        created = easyeffects_manager.create_peq_preset_from_rew_text(preset_name, rew_text, extras=extras)
        if load_after_create:
            easyeffects_manager.load_preset(created["name"])
        status = easyeffects_manager.get_status()
        await manager.broadcast({"type": "easyeffects", "data": status})
        schedule_peak_monitor_refresh_after_effects_change("import-rew-peq")
        return {
            "status": "ok",
            "preset": created,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

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
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")

    if not preset_name.strip():
        raise HTTPException(status_code=400, detail="preset_name is required")

    extras = _resolve_effects_extras({
        "limiter": {"enabled": limiter_enabled},
        "headroom": {"enabled": headroom_enabled, "params": {"gainDb": headroom_gain_db}},
        "autogain": {"enabled": autogain_enabled, "params": {"targetDb": autogain_target_db}},
        "delay": {
            "enabled": delay_enabled,
            "params": {"leftMs": delay_left_ms, "rightMs": delay_right_ms},
        },
        "bass_enhancer": {
            "enabled": bass_enabled,
            "params": {"amount": bass_amount},
        },
        "tone_effect": {"enabled": tone_effect_enabled, "mode": tone_effect_mode},
    })

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

            created = easyeffects_manager.create_convolver_preset_with_dual_uploads(
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

            created = easyeffects_manager.create_dual_peq_preset_from_rew_texts(
                preset_name,
                left_text,
                right_text,
                extras=extras,
            )
            import_kind = "dual-peq"

        if load_after_create:
            easyeffects_manager.load_preset(created["preset"]["name"] if import_kind == "dual-convolver" else created["name"])
        status = easyeffects_manager.get_status()
        await manager.broadcast({"type": "easyeffects", "data": status})
        schedule_peak_monitor_refresh_after_effects_change("import-filter-dual")
        return {
            "status": "ok",
            "import_kind": import_kind,
            "preset": created["preset"] if import_kind == "dual-convolver" else created,
            "ir": created.get("ir") if isinstance(created, dict) else None,
            "loaded": bool(load_after_create),
            "active_preset": status.get("active_preset"),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for tmp_path in tmp_paths:
            tmp_path.unlink(missing_ok=True)

@app.post("/api/easyeffects/presets/delete")
async def delete_easyeffects_preset(request: Request):
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")

    try:
        body = await request.json()
        preset_name = (body.get("preset_name") or "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body, expected {\"preset_name\": \"...\"}")

    if not preset_name:
        raise HTTPException(status_code=400, detail="preset_name is required")

    try:
        easyeffects_manager.delete_preset(preset_name)
        status = easyeffects_manager.get_status()
        await manager.broadcast({"type": "easyeffects", "data": status})
        schedule_peak_monitor_refresh_after_effects_change("preset-delete")
        return {"status": "ok", "deleted": preset_name}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/library/refresh")
async def refresh_library():
    global library_scanner
    if library_scanner:
        library_scanner.refresh()
        tracks = library_scanner.get_tracks()
        return {"status": "scanning", "track_count": len(tracks)}
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
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, log_level=settings.LOG_LEVEL.lower(), reload=False)

if __name__ == "__main__":
    settings = get_settings()
    run_server()
