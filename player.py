# SPDX-License-Identifier: AGPL-3.0-only

"""MPV player wrapper using subprocess with JSON IPC."""

import asyncio
import inspect
import json
import logging
import os
import socket
import subprocess
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


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
        }
        self._callbacks = []
        self._last_end_reason: Optional[str] = None
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
        """Send a command to mpv via the JSON IPC socket."""
        if not self._running:
            raise MPVError("MPV is not running")

        msg = {"command": [command, *args], "request_id": int(time.time() * 1000)}

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self.socket_path)
                sock.settimeout(5)
                sock.sendall((json.dumps(msg) + "\n").encode())

                buffer = b""
                while b"\n" not in buffer:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
            finally:
                sock.close()

            if not buffer:
                return {}

            for line in buffer.decode(errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("request_id") == msg["request_id"]:
                    return payload

            return {}
        except Exception as e:
            logger.error(f"Failed to send command {command}: {e}")
            raise MPVError(f"IPC communication failed: {e}") from e

    def _event_listener_loop(self):
        """Listen for mpv property-change events on a dedicated IPC connection."""
        while self._running and self._listener_socket is None:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self.socket_path)
                sock.settimeout(1.0)
                self._listener_socket = sock
            except Exception as e:
                logger.debug(f"Waiting for mpv listener socket: {e}")
                time.sleep(0.2)

        if not self._listener_socket:
            return

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
                    changed = True

            elif name == "duration":
                duration = float(data or 0.0)
                if self._state.get("duration") != duration:
                    self._state["duration"] = duration
                    changed = True

            elif name == "volume":
                volume = int(round(data or self._state.get("volume", 100)))
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

    def loadfile(self, path: str, mode: str = "replace"):
        """Load a file/URL and start playback."""
        with self.lock:
            logger.info(f"Loading: {path} (mode: {mode})")
            result = self._send_command("loadfile", path, mode)
            self._last_end_reason = None
            self._state["playing"] = True
            self._state["paused"] = False
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

    def set_loop_playlist(self, enabled: bool):
        """Enable or disable mpv playlist looping."""
        with self.lock:
            return self.set_property("loop-playlist", "inf" if enabled else "no")

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
