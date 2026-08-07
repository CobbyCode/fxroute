# SPDX-License-Identifier: AGPL-3.0-only

"""MPV player wrapper using subprocess with JSON IPC."""

import asyncio
import inspect
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Bounded backoff for the mpv event-listener reconnect: the listener never
# spins in a tight loop. After a broken socket/read it waits, then retries,
# doubling up to the cap until the connection is established again.
LISTENER_RECONNECT_DELAY_INITIAL = 0.2
LISTENER_RECONNECT_DELAY_MAX = 5.0

# Normalization of live stream facts (mpv property values, not URL guesses).
LOSSLESS_CODECS = {"flac", "alac", "ape", "wavpack", "tta"}
STREAM_CODEC_LABELS = {
    "aac": "AAC", "mp3": "MP3", "flac": "FLAC", "vorbis": "Vorbis",
    "opus": "Opus", "alac": "ALAC", "pcm": "PCM",
}
# Decoded sample format -> source bit depth for lossless codecs (ffmpeg
# decoder convention: 16-bit FLAC decodes to s16, 24-bit to s32, 32-bit to s64;
# PCM depth is parsed from the codec name instead).
FORMAT_BIT_DEPTH = {"s16": 16, "s24": 24, "s32": 24, "s64": 32}


def _bit_depth_from_codec_name(codec_name: str) -> Optional[int]:
    """Parse an explicit bit depth from the mpv codec name (e.g. PCM)."""
    m = re.search(r"(\d+)-bit", codec_name)
    return int(m.group(1)) if m else None


def normalize_stream_info(raw: dict) -> Optional[dict]:
    """Reduce raw mpv stream audio facts to the compact display form.

    Only values mpv actually delivered are kept. Unknown parts are omitted
    entirely; no placeholder text and no URL/file-extension guessing.
    """
    if not raw:
        return None
    codec_name = str(raw.get("codec") or "").strip()
    short = codec_name.split()[0].lower() if codec_name else ""
    if not short:
        # No format anchor: a bare bitrate would produce a misleading line.
        return None
    info: dict = {"codec": STREAM_CODEC_LABELS.get(short, short.upper())}
    if short in LOSSLESS_CODECS:
        info["profile"] = "Lossless"
    bitrate = raw.get("bitrate_bps")
    if isinstance(bitrate, (int, float)) and bitrate > 0:
        info["bitrate_kbps"] = int(round(bitrate / 1000))
    samplerate = raw.get("samplerate_hz")
    if isinstance(samplerate, int) and samplerate > 0:
        info["samplerate_hz"] = samplerate
    # Bit depth: explicit in PCM codec names; for lossless codecs derive from
    # the decoded sample format. Lossy decodes (floatp) have no source depth.
    depth = _bit_depth_from_codec_name(codec_name)
    if depth is None and short in LOSSLESS_CODECS:
        depth = FORMAT_BIT_DEPTH.get(str(raw.get("format") or ""))
    if depth and depth > 0:
        info["bit_depth"] = depth
    return info if info else None


class MPVError(Exception):
    """Base exception for MPV-related errors."""


class MPVNotInstalledError(MPVError):
    """MPV is not installed on the system."""


class MPVWrapper:
    """Thread-safe wrapper around a single mpv instance using JSON IPC."""

    def __init__(self):
        self.socket_path = "/tmp/mpv.sock"
        self.process: Optional[subprocess.Popen] = None
        self.lock = threading.RLock()
        self._running = False
        self._state = {
            "playing": False,
            "paused": False,
            "position": 0.0,
            "duration": 0.0,
            "volume": 100,
            "current_file": None,
            "playlist_pos": None,
            "ended": False,
            "error": None,
            "_seq": 0,
        }
        self._callbacks = []
        self._last_end_reason: Optional[str] = None
        self._last_position_notify_at = 0.0
        self._last_position_notify_position = 0.0
        self._listener_socket: Optional[socket.socket] = None
        self._listener_thread: Optional[threading.Thread] = None
        self._observer_ids = {
            "pause": 1,
            "time-pos": 2,
            "duration": 3,
            "volume": 4,
            "idle-active": 5,
            "path": 6,
            "playlist-pos": 7,
        }

    def start(self):
        """Start the mpv subprocess with IPC server."""
        if self._running:
            logger.warning("MPV already running")
            return

        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

        try:
            subprocess.run(["mpv", "--version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            raise MPVNotInstalledError("mpv is not installed or not in PATH") from e

        cmd = [
            "mpv",
            "--idle=yes",
            "--input-ipc-server=" + self.socket_path,
            "--no-video",
            "--quiet",
            "--network-timeout=15",
            "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_at_eof=1,reconnect_delay_max=5",
        ]
        logger.info(f"Starting mpv: {' '.join(cmd)}")
        self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        timeout = 5
        start = time.time()
        while not os.path.exists(self.socket_path):
            if time.time() - start > timeout:
                self.stop()
                raise MPVError(f"MPV socket not created after {timeout}s")
            time.sleep(0.1)

        self._running = True
        logger.info("MPV started successfully")

        self._listener_thread = threading.Thread(target=self._event_listener_loop, daemon=True)
        self._listener_thread.start()

    def stop(self):
        """Stop the mpv subprocess."""
        self._running = False

        if self._listener_socket:
            try:
                self._listener_socket.close()
            except Exception:
                pass
            self._listener_socket = None

        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        logger.info("MPV stopped")

    def _send_command(self, command: str, *args) -> Dict[str, Any]:
        """Send a command to mpv via the JSON IPC socket.

        mpv may emit asynchronous events (e.g. start-file) on the command
        connection before the command's response; the reader keeps consuming
        lines until the matching request_id arrives instead of stopping at
        the first newline.
        """
        if not self._running:
            raise MPVError("MPV is not running")

        msg = {"command": [command, *args], "request_id": int(time.time() * 1000)}

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self.socket_path)
                sock.settimeout(5)
                sock.sendall((json.dumps(msg) + "\n").encode())

                deadline = time.time() + 5
                buffer = b""
                while True:
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if payload.get("request_id") == msg["request_id"]:
                            response_error = payload.get("error")
                            if response_error not in (None, "success"):
                                raise MPVError(f"MPV command {command} failed: {response_error}")
                            return payload
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise socket.timeout
                    sock.settimeout(remaining)
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
            finally:
                sock.close()

            raise MPVError(f"MPV returned no matching response for {command}")
        except MPVError:
            raise
        except socket.timeout:
            logger.error(f"Failed to send command {command}: timed out waiting for response")
            raise MPVError(f"IPC communication failed: {command} timed out") from None
        except Exception as e:
            logger.error(f"Failed to send command {command}: {e}")
            raise MPVError(f"IPC communication failed: {e}") from e

    def _event_listener_loop(self):
        """Listen for mpv property-change events on a dedicated IPC connection.

        The connection is re-established with a bounded backoff whenever the
        socket breaks or a read fails while the player is still running.
        """
        reconnect_delay = LISTENER_RECONNECT_DELAY_INITIAL
        while self._running:
            if self._listener_socket is None:
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(self.socket_path)
                    sock.settimeout(1.0)
                    self._listener_socket = sock
                    reconnect_delay = LISTENER_RECONNECT_DELAY_INITIAL
                except Exception as e:
                    logger.debug(f"Waiting for mpv listener socket: {e}")
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, LISTENER_RECONNECT_DELAY_MAX)
                    continue

            for prop, observer_id in self._observer_ids.items():
                try:
                    msg = {"command": ["observe_property", observer_id, prop], "request_id": int(time.time() * 1000)}
                    self._listener_socket.sendall((json.dumps(msg) + "\n").encode())
                except Exception as e:
                    logger.debug(f"Failed to register mpv listener property {prop}: {e}")

            buffer = ""
            while self._running and self._listener_socket:
                try:
                    chunk = self._listener_socket.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk.decode(errors="ignore")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        self._handle_event(event)
                except socket.timeout:
                    continue
                except Exception as e:
                    if self._running:
                        logger.warning(f"MPV event listener error: {e}")
                    break

            if self._listener_socket:
                try:
                    self._listener_socket.close()
                except Exception:
                    pass
                self._listener_socket = None

            if not self._running:
                break
            # Socket/read error or EOF while the player is still active:
            # reconnect after a bounded delay (never a tight loop).
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, LISTENER_RECONNECT_DELAY_MAX)

    def _handle_event(self, event: Dict[str, Any]):
        event_name = event.get("event")
        changed = False

        if event_name == "property-change":
            name = event.get("name")
            data = event.get("data")

            if name == "pause":
                paused = bool(data)
                if self._state.get("paused") != paused:
                    self._state["paused"] = paused
                    self._state["playing"] = (self._state.get("current_file") is not None) and not paused and not self._state.get("ended")
                    changed = True

            elif name == "time-pos":
                position = float(data or 0.0)
                if self._state.get("position") != position:
                    self._state["position"] = position
                    # mpv can emit time-pos very frequently. On slower boards each
                    # callback builds a full UI payload, so unthrottled position
                    # events can backlog and arrive after a later pause/play state.
                    # Keep local position current, but only broadcast coarse seek
                    # progress; explicit pause/play/path events still notify
                    # immediately through their own branches.
                    now = time.monotonic()
                    if (
                        now - self._last_position_notify_at >= 0.5
                        or abs(position - self._last_position_notify_position) >= 0.5
                    ):
                        self._last_position_notify_at = now
                        self._last_position_notify_position = position
                        changed = True

            elif name == "duration":
                duration = float(data or 0.0)
                if self._state.get("duration") != duration:
                    self._state["duration"] = duration
                    changed = True

            elif name == "volume":
                # data == 0 is a valid mute value and must not be discarded
                # by a truthiness fallback.
                volume = int(round(data)) if data is not None else self._state.get("volume", 100)
                if self._state.get("volume") != volume:
                    self._state["volume"] = volume
                    changed = True

            elif name == "path":
                current_file = data or None
                if self._state.get("current_file") != current_file:
                    self._state["current_file"] = current_file
                    self._state["position"] = 0.0
                    self._state["duration"] = 0.0
                    if current_file is None:
                        self._state["ended"] = self._last_end_reason in {"eof", "error"}
                        self._state["playing"] = False
                    else:
                        self._state["ended"] = False
                        self._state["playing"] = not self._state.get("paused")
                    changed = True

            elif name == "playlist-pos":
                playlist_pos = int(data) if isinstance(data, (int, float)) else None
                if self._state.get("playlist_pos") != playlist_pos:
                    self._state["playlist_pos"] = playlist_pos
                    changed = True

            elif name == "idle-active":
                idle_active = bool(data)
                if idle_active and self._state.get("current_file") is not None:
                    self._state["playing"] = False
                    self._state["paused"] = False
                    self._state["position"] = 0.0
                    self._state["duration"] = 0.0
                    self._state["current_file"] = None
                    self._state["playlist_pos"] = None
                    self._state["ended"] = self._last_end_reason in {"eof", "error"}
                    self._last_end_reason = None
                    changed = True
                elif not idle_active and self._state.get("current_file") is not None:
                    next_playing = not self._state.get("paused") and not self._state.get("ended")
                    if self._state.get("playing") != next_playing:
                        self._state["playing"] = next_playing
                        changed = True

        elif event_name == "end-file":
            self._last_end_reason = event.get("reason")

        if changed:
            self._notify_callbacks()

    def loadfile(self, path: str, mode: str = "replace", *, start_paused: bool | None = None):
        """Load a file/URL, optionally preserving or explicitly setting pause.

        A transition may stage a target while the output gate is closed.  The
        old implementation unconditionally rewrote the cached state to
        ``paused=False`` immediately after ``loadfile``; callbacks could then
        observe a playing target before the coordinator had verified the graph.
        ``None`` preserves the cached pause state, while callers that own the
        transition can explicitly request a paused load.
        """
        with self.lock:
            logger.info(f"Loading: {path} (mode: {mode})")
            result = self._send_command("loadfile", path, mode)
            # Appending to an mpv playlist must not masquerade as an active
            # track change.  The previous implementation overwrote
            # current_file for every appended entry, leaving the player state
            # on the last queued file until a later path event happened.
            if mode == "append":
                return result
            if start_paused is None:
                start_paused = bool(self._state.get("paused"))
            self._last_end_reason = None
            self._state["playing"] = not bool(start_paused)
            self._state["paused"] = bool(start_paused)
            self._state["current_file"] = path
            self._state["position"] = 0.0
            self._state["duration"] = 0.0
            self._state["ended"] = False
            self._notify_callbacks()
            return result

    def set_pause(self, paused: bool):
        """Set pause state explicitly."""
        with self.lock:
            result = self.set_property("pause", paused)
            self._state["paused"] = paused
            self._state["ended"] = False
            self._state["playing"] = not paused and self._state.get("current_file") is not None
            self._notify_callbacks()
            return result

    def pause(self):
        """Toggle pause."""
        with self.lock:
            new_paused = not self._state.get("paused", False)
            return self.set_pause(new_paused)

    def stop_playback(self):
        """Stop playback."""
        with self.lock:
            self._send_command("stop")
            self._last_end_reason = None
            self._state["playing"] = False
            self._state["paused"] = False
            self._state["position"] = 0.0
            self._state["duration"] = 0.0
            self._state["current_file"] = None
            self._state["playlist_pos"] = None
            self._state["ended"] = False
            self._notify_callbacks()

    def set_volume(self, volume: int):
        """Set volume (0-100)."""
        with self.lock:
            volume = max(0, min(100, volume))
            if self._state.get("volume") == volume:
                return {"volume": volume, "unchanged": True}
            result = self.set_property("volume", volume)
            self._state["volume"] = volume
            self._notify_callbacks()
            return result

    def get_property(self, name: str):
        """Get an mpv property value."""
        with self.lock:
            result = self._send_command("get_property", name)
            return result.get("data") if isinstance(result, dict) else None

    def set_property(self, name: str, value: Any):
        """Set an mpv property value."""
        with self.lock:
            return self._send_command("set_property", name, value)

    def set_playlist_pos(self, index: int):
        """Jump to an entry inside the active mpv playlist."""
        with self.lock:
            result = self.set_property("playlist-pos", index)
            self._state["playlist_pos"] = index
            self._state["ended"] = False
            self._notify_callbacks()
            return result

    def clear_playlist(self):
        """Remove queued entries while keeping the currently played file."""
        with self.lock:
            result = self._send_command("playlist-clear")
            self._state["playlist_pos"] = 0 if self._state.get("current_file") else None
            self._notify_callbacks()
            return result

    def set_loop_playlist(self, enabled: bool):
        """Enable or disable mpv playlist looping."""
        with self.lock:
            return self.set_property("loop-playlist", "inf" if enabled else "no")

    def set_shuffle(self, enabled: bool):
        """Enable or disable mpv's native playlist shuffle mode."""
        with self.lock:
            return self.set_property("shuffle", bool(enabled))

    def set_loop_file(self, enabled: bool):
        """Enable or disable mpv single-file looping."""
        with self.lock:
            return self.set_property("loop-file", "inf" if enabled else "no")

    def remove_playlist_index(self, index: int):
        """Remove a playlist entry by index without stopping playback."""
        with self.lock:
            return self._send_command("playlist-remove", index)

    def seek(self, position: float):
        """Seek to absolute position in seconds."""
        with self.lock:
            result = self._send_command("seek", position, "absolute")
            self._state["position"] = position
            self._notify_callbacks()
            return result

    def register_callbacks(self, callback):
        """Register a callback for state changes."""
        callback_loop = None
        if inspect.iscoroutinefunction(callback):
            try:
                callback_loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning("Registered async callback without a running event loop")
        self._callbacks.append((callback, callback_loop))

    def _notify_callbacks(self):
        """Notify all callbacks with current state."""
        self._state["_seq"] = int(self._state.get("_seq") or 0) + 1
        snapshot = self._state.copy()
        for callback, callback_loop in list(self._callbacks):
            try:
                if inspect.iscoroutinefunction(callback):
                    if not callback_loop or not callback_loop.is_running():
                        logger.warning("Skipping async callback dispatch because no running loop is available")
                        continue
                    callback_loop.call_soon_threadsafe(asyncio.create_task, callback(snapshot.copy()))
                else:
                    callback(snapshot.copy())
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def get_stream_audio_info(self) -> Dict[str, Any]:
        """Read live stream audio facts from mpv (codec, bitrate, sample rate).

        Returns only values mpv actually delivers; missing properties are
        omitted. Nothing is inferred from URLs or file extensions.
        """
        info: Dict[str, Any] = {}
        try:
            codec = self.get_property("audio-codec")
            if isinstance(codec, str) and codec.strip():
                info["codec"] = codec.strip()
        except Exception as exc:
            logger.debug("Failed to read mpv audio-codec: %s", exc)
        try:
            bitrate = self.get_property("audio-bitrate")
            if isinstance(bitrate, (int, float)) and bitrate > 0:
                info["bitrate_bps"] = int(bitrate)
        except Exception as exc:
            logger.debug("Failed to read mpv audio-bitrate: %s", exc)
        try:
            params = self.get_property("audio-params")
            if isinstance(params, dict):
                rate = params.get("samplerate")
                if isinstance(rate, int) and rate > 0:
                    info["samplerate_hz"] = rate
                fmt = params.get("format")
                if isinstance(fmt, str) and fmt.strip():
                    info["format"] = fmt.strip()
        except Exception as exc:
            logger.debug("Failed to read mpv audio-params: %s", exc)
        return info

    def get_metadata(self) -> Dict[str, Any]:
        """Query mpv for current stream metadata (ICY tags, etc.)."""
        try:
            result = self._send_command("get_property", "metadata")
            if result and "data" in result:
                return result["data"] or {}
        except Exception as e:
            logger.debug(f"Metadata query failed: {e}")
        return {}

    @property
    def state(self) -> Dict[str, Any]:
        """Get current state."""
        return self._state.copy()


player: Optional[MPVWrapper] = None


def get_player() -> MPVWrapper:
    """Get or create the global player instance."""
    global player
    if player is None:
        player = MPVWrapper()
    return player
