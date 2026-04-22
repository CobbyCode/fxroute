# SPDX-License-Identifier: AGPL-3.0-only

"""Main FastAPI application for FXRoute."""

import json
import logging
import shutil
import time
import asyncio
import random
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import get_settings

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# Cooldown to prevent rapid mpv IPC flooding (ms)
PLAY_COMMAND_COOLDOWN_MS = 400
LOCAL_TRACK_SWITCH_SETTLE_MS = 180
SOURCE_HANDOFF_SETTLE_MS = 180
RADIO_SAMPLERATE_RENEGOTIATE_DELAY_MS = 1200
RADIO_SAMPLERATE_PRESET_BOUNCE_DELAY_MS = 350

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

from models import (
    DeleteTracksRequest,
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
from peak_monitor import EasyEffectsPeakMonitor
from samplerate import get_samplerate_status
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
peak_monitor = None
peak_monitor_playback_armed = False
peak_monitor_transition_lock = None
peak_monitor_context_signature = None
easyeffects_preset_load_lock = None
source_transition_lock = None
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
    return (
        live_track.get("source") == expected_track.get("source")
        and live_track.get("url") == expected_track.get("url")
        and live_track.get("id") == expected_track.get("id")
    )


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


async def _maybe_renegotiate_radio_samplerate(expected_track: dict | None) -> None:
    global easyeffects_manager, easyeffects_preset_load_lock
    if not expected_track or expected_track.get("source") != "radio":
        return
    if not easyeffects_manager or not player_instance or not player_instance._running:
        return

    await asyncio.sleep(RADIO_SAMPLERATE_RENEGOTIATE_DELAY_MS / 1000)

    if not _current_track_matches(expected_track):
        return

    mpv_rate = _get_player_audio_samplerate()
    if not mpv_rate:
        return

    try:
        samplerate_status = get_samplerate_status()
    except Exception as exc:
        logger.debug("Radio samplerate renegotiation check failed: %s", exc)
        return

    sink_rate = samplerate_status.get("active_rate")
    if not isinstance(sink_rate, int) or sink_rate <= 0 or sink_rate == mpv_rate:
        return

    active_preset = easyeffects_manager.get_active_preset()
    if not active_preset:
        return

    bounce_preset = "Neutral" if active_preset != "Neutral" else "Direct"
    logger.info(
        "Radio samplerate mismatch detected, bouncing EasyEffects preset via %s: preset=%s mpv_rate=%s sink_rate=%s track=%s",
        bounce_preset,
        active_preset,
        mpv_rate,
        sink_rate,
        expected_track.get("url"),
    )

    if easyeffects_preset_load_lock is None:
        easyeffects_preset_load_lock = asyncio.Lock()

    try:
        async with easyeffects_preset_load_lock:
            if not _current_track_matches(expected_track):
                return
            easyeffects_manager.load_preset(bounce_preset)
            await asyncio.sleep(RADIO_SAMPLERATE_PRESET_BOUNCE_DELAY_MS / 1000)
            if not _current_track_matches(expected_track):
                return
            easyeffects_manager.load_preset(active_preset)
            status = easyeffects_manager.get_status()
        await manager.broadcast({"type": "easyeffects", "data": status})
        await refresh_peak_monitor_after_effects_change("radio-samplerate-renegotiate")
        final_status = get_samplerate_status()
        logger.info(
            "Radio samplerate renegotiation finished: preset=%s final_sink_rate=%s mpv_rate=%s",
            active_preset,
            final_status.get("active_rate"),
            mpv_rate,
        )
    except Exception as exc:
        logger.warning("Radio samplerate renegotiation failed for %s: %s", expected_track.get("url"), exc)


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
        logger.info(
            "Applying hard handoff before %s (%s): %s -> %s",
            transition_reason,
            handoff_reason,
            previous_file,
            next_url,
        )
        player_instance.stop_playback()
        settle_ms = LOCAL_TRACK_SWITCH_SETTLE_MS if handoff_reason == "manual local track switch" else SOURCE_HANDOFF_SETTLE_MS
        await asyncio.sleep(settle_ms / 1000)

    queue_transition_target_url = next_url
    try:
        player_instance.loadfile(next_url, mode="replace")
        player_instance.set_pause(False)
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
        is_active_playback = bool(state.get("current_file") and not state.get("paused") and not state.get("ended"))
        source = (current_track_info or {}).get("source") or "unknown"
        desired_signature = f"player:{source}:{state.get('current_file') or ''}" if is_active_playback else None

        if is_active_playback and (not peak_monitor_playback_armed or peak_monitor_context_signature != desired_signature):
            peak_monitor_playback_armed = True
            peak_monitor_context_signature = desired_signature
            logger.info("Restarting peak monitor on playback context change to refresh PipeWire links: %s", desired_signature)
            await peak_monitor.restart()
            await manager.broadcast({"type": "playback_peak_warning", "data": peak_monitor.snapshot()})
        elif not is_active_playback and peak_monitor_playback_armed:
            spotify_state = await get_spotify_ui_state()
            if spotify_state.get("available") and spotify_state.get("status") == "Playing":
                return
            logger.info("Stopping peak monitor while playback is inactive")
            await peak_monitor.stop()
            peak_monitor_playback_armed = False
            peak_monitor_context_signature = None
            await manager.broadcast({"type": "playback_peak_warning", "data": peak_monitor.snapshot()})


async def sync_peak_monitor_for_spotify_state(data: dict):
    global peak_monitor_playback_armed, peak_monitor, player_instance, peak_monitor_transition_lock, peak_monitor_context_signature
    if not peak_monitor:
        return
    if peak_monitor_transition_lock is None:
        peak_monitor_transition_lock = asyncio.Lock()

    async with peak_monitor_transition_lock:
        player_state = player_instance.state if player_instance else {}
        is_spotify_playing = bool(data.get("available") and data.get("status") == "Playing")
        desired_signature = "spotify:playing" if is_spotify_playing else None

        if is_spotify_playing and (not peak_monitor_playback_armed or peak_monitor_context_signature != desired_signature):
            peak_monitor_playback_armed = True
            peak_monitor_context_signature = desired_signature
            logger.info("Starting peak monitor for active Spotify playback")
            await peak_monitor.restart()
            await manager.broadcast({"type": "playback_peak_warning", "data": peak_monitor.snapshot()})
        elif not is_spotify_playing and peak_monitor_playback_armed:
            if not (player_state.get("current_file") and not player_state.get("paused") and not player_state.get("ended")):
                logger.info("Stopping peak monitor because Spotify is no longer actively playing")
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
    await asyncio.sleep(0.25)

    if is_spotify_playing:
        await sync_peak_monitor_for_spotify_state(spotify_state)
    elif is_local_playing:
        await sync_peak_monitor_for_playback_state(player_state)


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
                player_instance.loadfile(current_track_info["url"], mode="replace")
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
    data = await get_spotify_ui_state(data)
    await sync_peak_monitor_for_spotify_state(data)
    await manager.broadcast({"type": "spotify", "data": data})
    return data


async def pause_spotify_for_local_playback_broadcast():
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
    global player_instance
    try:
        if player_instance and player_instance._running:
            player_instance.stop_playback()
            await manager.broadcast({"type": "playback", "data": build_playback_payload(player_instance.state)})
            await asyncio.sleep(0.2)
    except Exception:
        pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown."""
    global settings, player_instance, library_scanner, downloader, easyeffects_manager, peak_monitor, peak_monitor_playback_armed, peak_monitor_transition_lock, peak_monitor_context_signature, easyeffects_preset_load_lock, source_transition_lock

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

        peak_monitor = EasyEffectsPeakMonitor(on_change=on_peak_monitor_change)
        peak_monitor_playback_armed = False
        peak_monitor_transition_lock = asyncio.Lock()
        peak_monitor_context_signature = None
        easyeffects_preset_load_lock = asyncio.Lock()
        source_transition_lock = asyncio.Lock()
        logger.info("EasyEffects output peak monitor initialized")

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
    if peak_monitor:
        await peak_monitor.stop()
        logger.info("EasyEffects output peak monitor stopped")

app = FastAPI(lifespan=lifespan)

# Static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/", response_class=HTMLResponse)
async def read_root():
    return HTMLResponse(content=(STATIC_DIR / "index.html").read_text())

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
    global player_instance, current_track_info, last_track_info, source_transition_lock
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

            current_track_info = track_info
            last_track_info = track_info

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
                    logger.info(
                        "Applying hard handoff before play (%s): %s -> %s",
                        handoff_reason,
                        previous_file,
                        play_url,
                    )
                    player_instance.stop_playback()
                    settle_ms = LOCAL_TRACK_SWITCH_SETTLE_MS if handoff_reason == "manual local track switch" else SOURCE_HANDOFF_SETTLE_MS
                    await asyncio.sleep(settle_ms / 1000)
                if playback_queue_mode == "mpv_native" and len(playback_queue) > 1:
                    if not _prime_mpv_native_queue(playback_queue_index):
                        raise HTTPException(status_code=500, detail="Failed to initialize native mpv playlist")
                else:
                    player_instance.loadfile(play_url, mode="replace")
                    # Ensure MPV is unpaused after loadfile (it may stay paused if previously paused by Spotify)
                    player_instance.set_pause(False)

            if source == "radio":
                asyncio.create_task(_maybe_renegotiate_radio_samplerate(track_info.copy()))

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
    player_instance.loadfile(replay_url, mode="replace")
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
    return get_samplerate_status()

def _parse_effects_extras_from_json(body: dict) -> dict:
    limiter_enabled = bool(body.get("limiterEnabled", body.get("limiter_enabled", False)))
    headroom_enabled = bool(body.get("headroomEnabled", body.get("headroom_enabled", False)))
    headroom_gain_db = float(body.get("headroomGainDb", body.get("headroom_gain_db", -3.0)) or -3.0)
    delay_enabled = bool(body.get("delayEnabled", body.get("delay_enabled", False)))
    delay_left_ms = float(body.get("delayLeftMs", body.get("delay_left_ms", 0.0)) or 0.0)
    delay_right_ms = float(body.get("delayRightMs", body.get("delay_right_ms", 0.0)) or 0.0)
    bass_enabled = bool(body.get("bassEnabled", body.get("bass_enabled", False)))
    bass_amount = float(body.get("bassAmount", body.get("bass_amount", 0.0)) or 0.0)
    return {
        "limiter": {"enabled": limiter_enabled},
        "headroom": {
            "enabled": headroom_enabled,
            "params": {
                "gainDb": headroom_gain_db,
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
        await refresh_peak_monitor_after_effects_change("preset-load")
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
        await refresh_peak_monitor_after_effects_change("ir-upload")
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
    delay_enabled: bool = Form(False),
    delay_left_ms: float = Form(0.0),
    delay_right_ms: float = Form(0.0),
):
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")

    extras = _resolve_effects_extras({
        "limiter": {"enabled": limiter_enabled},
        "headroom": {"enabled": headroom_enabled, "params": {"gainDb": headroom_gain_db}},
        "delay": {
            "enabled": delay_enabled,
            "params": {"leftMs": delay_left_ms, "rightMs": delay_right_ms},
        },
    })

    try:
        created = easyeffects_manager.create_convolver_preset(preset_name, ir_filename, extras=extras)
        if load_after_create:
            easyeffects_manager.load_preset(created["name"])
        status = easyeffects_manager.get_status()
        await manager.broadcast({"type": "easyeffects", "data": status})
        await refresh_peak_monitor_after_effects_change("create-convolver")
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

@app.post("/api/easyeffects/presets/create-with-ir")
async def create_convolver_preset_with_ir(
    preset_name: str = Form(...),
    load_after_create: bool = Form(False),
    limiter_enabled: bool = Form(False),
    headroom_enabled: bool = Form(False),
    headroom_gain_db: float = Form(-3.0),
    delay_enabled: bool = Form(False),
    delay_left_ms: float = Form(0.0),
    delay_right_ms: float = Form(0.0),
    bass_enabled: bool = Form(False),
    bass_amount: float = Form(0.0),
    file: UploadFile = File(...),
):
    global easyeffects_manager
    if not easyeffects_manager:
        raise HTTPException(status_code=503, detail="EasyEffects manager not available")

    extras = _resolve_effects_extras({
        "limiter": {"enabled": limiter_enabled},
        "headroom": {"enabled": headroom_enabled, "params": {"gainDb": headroom_gain_db}},
        "delay": {
            "enabled": delay_enabled,
            "params": {"leftMs": delay_left_ms, "rightMs": delay_right_ms},
        },
        "bass_enhancer": {
            "enabled": bass_enabled,
            "params": {"amount": bass_amount},
        },
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
        await refresh_peak_monitor_after_effects_change("create-with-ir")
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
        await refresh_peak_monitor_after_effects_change("create-peq")
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
    delay_enabled: bool = Form(False),
    delay_left_ms: float = Form(0.0),
    delay_right_ms: float = Form(0.0),
    bass_enabled: bool = Form(False),
    bass_amount: float = Form(0.0),
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
        "delay": {
            "enabled": delay_enabled,
            "params": {"leftMs": delay_left_ms, "rightMs": delay_right_ms},
        },
        "bass_enhancer": {
            "enabled": bass_enabled,
            "params": {"amount": bass_amount},
        },
    })

    try:
        created = easyeffects_manager.create_peq_preset_from_rew_text(preset_name, rew_text, extras=extras)
        if load_after_create:
            easyeffects_manager.load_preset(created["name"])
        status = easyeffects_manager.get_status()
        await manager.broadcast({"type": "easyeffects", "data": status})
        await refresh_peak_monitor_after_effects_change("import-rew-peq")
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
    delay_enabled: bool = Form(False),
    delay_left_ms: float = Form(0.0),
    delay_right_ms: float = Form(0.0),
    bass_enabled: bool = Form(False),
    bass_amount: float = Form(0.0),
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
        "delay": {
            "enabled": delay_enabled,
            "params": {"leftMs": delay_left_ms, "rightMs": delay_right_ms},
        },
        "bass_enhancer": {
            "enabled": bass_enabled,
            "params": {"amount": bass_amount},
        },
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
        await refresh_peak_monitor_after_effects_change("import-filter-dual")
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
        await refresh_peak_monitor_after_effects_change("preset-delete")
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
    return await get_spotify_ui_state()


@app.post("/api/spotify/play")
async def api_spotify_play():
    global source_transition_lock
    if source_transition_lock is None:
        source_transition_lock = asyncio.Lock()
    async with source_transition_lock:
        # Source exclusivity: pause MPV when Spotify starts
        await pause_local_playback_for_spotify_broadcast()
        data = await spotify_play()
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
        # Source exclusivity: pause MPV when Spotify is about to play
        sd = await get_spotify_ui_state()
        if sd.get("status") != "Playing":
            await pause_local_playback_for_spotify_broadcast()
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
