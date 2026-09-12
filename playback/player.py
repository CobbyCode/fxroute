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

from playback.mpv_process import _stop_orphan_mpv_processes

logger = logging.getLogger(__name__)

# Bounded backoff for the mpv event-listener reconnect: the listener never
# spins in a tight loop. After a broken socket/read it waits, then retries,
# doubling up to the cap until the connection is established again.
LISTENER_RECONNECT_DELAY_INITIAL = 0.2
LISTENER_RECONNECT_DELAY_MAX = 5.0


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
            "file_loaded": False,
            "playlist_pos": None,
            "ended": False,
            "error": None,
            "end_reason": None,
            "end_entry_id": None,
            "_seq": 0,
        }
        # Last published coherent state snapshot.  ``state`` serves this
        # without taking the command lock, so event-loop readers never queue
        # behind a blocking IPC command; every mutation path publishes a new
        # snapshot under the lock in _notify_callbacks.
        self._state_snapshot: Dict[str, Any] = dict(self._state)
        self._callbacks = []
        self._callback_tasks: set[asyncio.Task] = set()
        self._next_callback_token = 0
        self._last_end_reason: Optional[str] = None
        self._last_end_entry_id: Optional[int] = None
        self._last_position_notify_at = 0.0
        self._last_position_notify_position = 0.0
        self._listener_socket: Optional[socket.socket] = None
        self._listener_thread: Optional[threading.Thread] = None
        self._listener_stop_event = threading.Event()
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

        # A previous killed service run may have left FXRoute-owned mpv
        # processes behind; they would compete for the same IPC socket and
        # PipeWire node.  Clean them up before spawning the new instance.
        _stop_orphan_mpv_processes(self.socket_path, own_pid=None)

        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

        try:
            subprocess.run(["mpv", "--version"], capture_output=True, check=True, timeout=15)
        except subprocess.TimeoutExpired as e:
            raise MPVNotInstalledError("mpv version probe timed out") from e
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            raise MPVNotInstalledError("mpv is not installed or not in PATH") from e

        cmd = self._mpv_command()
        logger.info(f"Starting mpv: {' '.join(cmd)}")
        self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        timeout = 15
        start = time.time()
        while not os.path.exists(self.socket_path):
            if time.time() - start > timeout:
                self.stop()
                raise MPVError(f"MPV socket not created after {timeout}s")
            time.sleep(0.1)

        self._running = True
        self._listener_stop_event.clear()
        logger.info("MPV started successfully")

        self._listener_thread = threading.Thread(target=self._event_listener_loop, daemon=True)
        self._listener_thread.start()

    @staticmethod
    def _mpv_command(socket_path: str = "/tmp/mpv.sock") -> list[str]:
        """The exact FXRoute mpv invocation (also the orphan-match pattern).

        mpv 0.41 removed an autoconnect switch, so the direct hardware link
        (DSP bypass) is prevented by pinning the audio device to the
        FXRoute ingress sink: the mpv stream can then only ever connect to
        ``fxroute_dsp_sink``, never to the hardware sink on its own.
        """
        return [
            "mpv",
            "--idle=yes",
            "--input-ipc-server=" + socket_path,
            "--no-video",
            "--quiet",
            "--network-timeout=15",
            "--audio-device=pipewire/fxroute_dsp_sink",
            "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_at_eof=1,reconnect_delay_max=5",
        ]

    def stop(self):
        """Stop the mpv subprocess."""
        self._running = False
        self._listener_stop_event.set()

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
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("MPV process did not exit after SIGKILL; leaving it to the startup orphan cleanup")
        listener_thread = self._listener_thread
        if listener_thread and listener_thread is not threading.current_thread():
            listener_thread.join(timeout=2)
        if listener_thread and listener_thread.is_alive():
            raise MPVError("MPV listener thread did not stop")
        self._listener_thread = None
        self.process = None
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
                    self._listener_stop_event.wait(reconnect_delay)
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
            self._listener_stop_event.wait(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, LISTENER_RECONNECT_DELAY_MAX)

    def _handle_event(self, event: Dict[str, Any]):
        with self.lock:
            self._handle_event_locked(event)

    def _handle_event_locked(self, event: Dict[str, Any]):
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
                    if current_file is None and self._state.get("playing") and self._last_end_reason not in {"eof", "error"}:
                        # mpv cleared an active source without an end-file or
                        # an explicit app stop: the "silent-but-playing" race.
                        # Capture full context so the next occurrence is debuggable.
                        logger.warning(
                            "MPV unexpectedly cleared the active source: path=%s paused=%s position=%.3f duration=%.3f end_reason=%s",
                            self._state.get("current_file"),
                            self._state.get("paused"),
                            float(self._state.get("position") or 0.0),
                            float(self._state.get("duration") or 0.0),
                            self._last_end_reason,
                        )
                    self._state["current_file"] = current_file
                    self._state["position"] = 0.0
                    self._state["duration"] = 0.0
                    if current_file is None:
                        self._state["ended"] = self._last_end_reason in {"eof", "error"}
                        self._state["end_reason"] = self._last_end_reason
                        self._state["end_entry_id"] = self._last_end_entry_id
                        self._state["file_loaded"] = False
                        self._state["playing"] = False
                    else:
                        self._state["ended"] = False
                        self._state["end_reason"] = None
                        self._state["end_entry_id"] = None
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
                    if self._state.get("playing") and self._last_end_reason not in {"eof", "error"}:
                        logger.warning(
                            "MPV unexpectedly unloaded the active source: path=%s paused=%s position=%.3f duration=%.3f end_reason=%s",
                            self._state.get("current_file"),
                            self._state.get("paused"),
                            float(self._state.get("position") or 0.0),
                            float(self._state.get("duration") or 0.0),
                            self._last_end_reason,
                        )
                    self._state["playing"] = False
                    self._state["paused"] = False
                    self._state["position"] = 0.0
                    self._state["duration"] = 0.0
                    self._state["current_file"] = None
                    self._state["file_loaded"] = False
                    self._state["playlist_pos"] = None
                    self._state["ended"] = self._last_end_reason in {"eof", "error"}
                    self._state["end_reason"] = self._last_end_reason
                    self._state["end_entry_id"] = self._last_end_entry_id
                    self._last_end_reason = None
                    self._last_end_entry_id = None
                    changed = True
                elif not idle_active and self._state.get("current_file") is not None:
                    next_playing = not self._state.get("paused") and not self._state.get("ended")
                    if self._state.get("playing") != next_playing:
                        self._state["playing"] = next_playing
                        changed = True

        elif event_name == "file-loaded":
            # mpv's canonical "file is loaded" event.  It fires for every file
            # or stream that opens, including live/unknown-duration sources
            # that never report a positive ``duration``.
            if not self._state.get("file_loaded"):
                self._state["file_loaded"] = True
                changed = True

        elif event_name == "end-file":
            # MPV IPC provides reason and playlist_entry_id (since mpv 0.33);
            # there is no file/filename field. The entry ID is the only
            # track identity MPV exposes and is kept for diagnostics/logging.
            self._last_end_reason = event.get("reason")
            entry_id = event.get("playlist_entry_id")
            self._last_end_entry_id = entry_id if isinstance(entry_id, int) else None

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
            self._last_end_entry_id = None
            self._state["playing"] = not bool(start_paused)
            self._state["paused"] = bool(start_paused)
            self._state["current_file"] = path
            self._state["file_loaded"] = False
            self._state["position"] = 0.0
            self._state["duration"] = 0.0
            self._state["ended"] = False
            self._state["end_reason"] = None
            self._state["end_entry_id"] = None
            self._notify_callbacks()
            return result

    def set_pause(self, paused: bool):
        """Set pause state explicitly."""
        with self.lock:
            result = self.set_property("pause", paused)
            self._state["paused"] = paused
            self._state["ended"] = False
            self._state["end_reason"] = None
            self._state["end_entry_id"] = None
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
            self._last_end_entry_id = None
            self._state["playing"] = False
            self._state["paused"] = False
            self._state["position"] = 0.0
            self._state["duration"] = 0.0
            self._state["current_file"] = None
            self._state["file_loaded"] = False
            self._state["playlist_pos"] = None
            self._state["ended"] = False
            self._state["end_reason"] = None
            self._state["end_entry_id"] = None
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
            self._state["end_reason"] = None
            self._state["end_entry_id"] = None
            self._notify_callbacks()
            return result

    def move_playlist_entry(self, old_index: int, new_index: int):
        """Move an entry without reloading the currently playing source."""
        with self.lock:
            return self._send_command("playlist-move", old_index, new_index)

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
        """Register a callback for state changes.

        Coroutine functions are invoked through a scheduled task.  A plain
        (sync) callback is invoked synchronously at notify time; if it
        returns an awaitable, that awaitable is scheduled as a task on the
        captured loop, exactly like a coroutine-function callback.  This
        lets a sync dispatcher capture context (e.g. a playback ownership
        token) at the moment the state change is observed, before the
        coroutine task is queued.
        """
        callback_loop = None
        try:
            callback_loop = asyncio.get_running_loop()
        except RuntimeError:
            if inspect.iscoroutinefunction(callback):
                logger.warning("Registered async callback without a running event loop")
        self._next_callback_token += 1
        self._callbacks.append((callback, callback_loop, self._next_callback_token))

    def unregister_callbacks(self, callback):
        """Remove every registration for one callback."""
        self._callbacks = [item for item in self._callbacks if item[0] is not callback]

    async def shutdown_callbacks(self, callback):
        """Stop new callback dispatch and drain tasks already queued on this loop."""
        self.unregister_callbacks(callback)
        await asyncio.sleep(0)
        tasks = [task for task in self._callback_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule_callback_task(self, callback, state, token):
        if not any(item[2] == token for item in self._callbacks):
            return
        task = asyncio.create_task(callback(state))
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_tasks.discard)

    def _schedule_awaitable_task(self, awaitable, token):
        if not any(item[2] == token for item in self._callbacks):
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            return
        task = asyncio.create_task(awaitable)
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_tasks.discard)

    def _notify_callbacks(self):
        """Notify all callbacks with current state."""
        with self.lock:
            self._state["_seq"] = int(self._state.get("_seq") or 0) + 1
            self._state_snapshot = self._state.copy()
        snapshot = self._state_snapshot.copy()
        for callback, callback_loop, token in list(self._callbacks):
            try:
                if inspect.iscoroutinefunction(callback):
                    if not callback_loop or not callback_loop.is_running():
                        logger.warning("Skipping async callback dispatch because no running loop is available")
                        continue
                    callback_loop.call_soon_threadsafe(
                        self._schedule_callback_task,
                        callback,
                        snapshot.copy(),
                        token,
                    )
                else:
                    result = callback(snapshot.copy())
                    if inspect.isawaitable(result):
                        if not callback_loop or not callback_loop.is_running():
                            logger.warning("Skipping awaitable callback result because no running loop is available")
                            close = getattr(result, "close", None)
                            if callable(close):
                                close()
                            continue
                        callback_loop.call_soon_threadsafe(
                            self._schedule_awaitable_task,
                            result,
                            token,
                        )
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
        """Get current state.

        Served from the last coherent published snapshot so readers never
        block on the command lock (a worker may hold it across IPC I/O).
        """
        return self._state_snapshot.copy()


player: Optional[MPVWrapper] = None


def get_player() -> MPVWrapper:
    """Get or create the global player instance."""
    global player
    if player is None:
        player = MPVWrapper()
    return player
