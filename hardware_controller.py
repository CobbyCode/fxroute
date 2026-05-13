# SPDX-License-Identifier: AGPL-3.0-only

"""Optional USB CDC hardware-controller support for FXRoute."""

from __future__ import annotations

import glob
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class HardwareController:
    """Small line-based serial client for an optional MCU controller.

    The controller is intentionally optional. Missing pyserial, missing devices,
    malformed replies, and serial I/O errors are reported in status but must not
    prevent FXRoute from running.
    """

    COMMANDS = {
        "PING",
        "GET",
        "SET INPUT RCA",
        "SET INPUT XLR",
        "PRESS INPUT",
        "AUTO ON",
        "AUTO OFF",
    }

    def __init__(self, device_path: Optional[str] = None, baudrate: int = 115200):
        self.device_path = str(device_path or "").strip() or None
        self.baudrate = baudrate
        self._serial = None
        self._connected_path: Optional[str] = None
        self._lock = threading.RLock()
        self._last_scan_at = 0.0
        self._last_log: dict[str, float] = {}
        self._latest_status: dict[str, Any] = {
            "connected": False,
            "device": None,
            "status": {},
            "raw": None,
            "notes": [],
        }

    def close(self):
        with self._lock:
            self._close_locked()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            if self._ensure_connected_locked():
                try:
                    self._send_command_locked("GET")
                except Exception as exc:
                    self._mark_error_locked("status read failed", exc)
            return self._snapshot_locked()

    def command(self, command: str) -> dict[str, Any]:
        command = command.strip().upper()
        if command not in self.COMMANDS or command in {"PING", "GET"}:
            return self._snapshot_with_error(f"unsupported command: {command or 'empty'}")
        with self._lock:
            if not self._ensure_connected_locked():
                return self._snapshot_locked()
            try:
                self._send_command_locked(command)
                self._send_command_locked("GET")
            except Exception as exc:
                self._mark_error_locked("command failed", exc)
            return self._snapshot_locked()

    def _snapshot_with_error(self, note: str) -> dict[str, Any]:
        with self._lock:
            snapshot = self._snapshot_locked()
            snapshot["notes"] = [note, *snapshot.get("notes", [])]
            return snapshot

    def _snapshot_locked(self) -> dict[str, Any]:
        status = dict(self._latest_status.get("status") or {})
        return {
            "available": True,
            "connected": bool(self._latest_status.get("connected")),
            "device": self._latest_status.get("device"),
            "status": status,
            "raw": self._latest_status.get("raw"),
            "power": status.get("POWER"),
            "trigger": status.get("TRIGGER"),
            "input": status.get("INPUT"),
            "rca": status.get("RCA"),
            "xlr": status.get("XLR"),
            "auto": status.get("AUTO"),
            "notes": list(self._latest_status.get("notes") or []),
        }

    def _ensure_connected_locked(self) -> bool:
        if self._serial is not None:
            return True
        now = time.monotonic()
        if now - self._last_scan_at < 2.0:
            return False
        self._last_scan_at = now
        return self._scan_locked()

    def _candidate_paths(self) -> list[str]:
        if self.device_path:
            return [self.device_path]
        return sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))

    def _scan_locked(self) -> bool:
        try:
            import serial  # type: ignore
        except Exception as exc:
            self._latest_status = {
                "connected": False,
                "device": None,
                "status": {},
                "raw": None,
                "notes": ["pyserial is not installed"],
            }
            self._log_throttled("pyserial-missing", logging.INFO, "Hardware controller unavailable: %s", exc)
            return False

        for path in self._candidate_paths():
            try:
                if not Path(path).exists():
                    continue
                ser = serial.Serial(path, self.baudrate, timeout=0.25, write_timeout=0.25)
                time.sleep(0.08)
                try:
                    ser.reset_input_buffer()
                except Exception:
                    pass
                self._serial = ser
                self._connected_path = path
                replies = self._send_command_locked("PING", expect_status=False)
                if any(line == "PONG" for line in replies):
                    self._latest_status = {
                        "connected": True,
                        "device": path,
                        "status": dict(self._latest_status.get("status") or {}),
                        "raw": self._latest_status.get("raw"),
                        "notes": [],
                    }
                    try:
                        self._send_command_locked("GET")
                    except Exception as exc:
                        self._mark_error_locked("initial status read failed", exc, keep_connected=True)
                    logger.info("Detected FXRoute hardware controller on %s", path)
                    return True
                self._close_locked()
            except Exception as exc:
                self._close_locked()
                self._log_throttled(f"scan-{path}", logging.DEBUG, "Hardware controller scan failed for %s: %s", path, exc)

        note = f"controller not detected at {self.device_path}" if self.device_path else "controller not detected"
        self._latest_status = {
            "connected": False,
            "device": None,
            "status": dict(self._latest_status.get("status") or {}),
            "raw": self._latest_status.get("raw"),
            "notes": [note],
        }
        return False

    def _send_command_locked(self, command: str, expect_status: bool = True) -> list[str]:
        if self._serial is None:
            raise RuntimeError("serial device is not connected")
        self._serial.write(f"{command}\n".encode("ascii"))
        self._serial.flush()
        deadline = time.monotonic() + 1.0
        replies: list[str] = []
        while time.monotonic() < deadline:
            raw = self._serial.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            replies.append(line)
            if line.startswith("ERR"):
                raise RuntimeError(line)
            if line == "OK" and not expect_status:
                break
            if line == "PONG":
                break
            if self._parse_status_line_locked(line):
                if expect_status:
                    break
            elif line == "OK" and command != "GET":
                break
        return replies

    def _parse_status_line_locked(self, line: str) -> bool:
        if "=" not in line or ";" not in line:
            return False
        parsed: dict[str, Any] = {}
        for part in line.split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip().upper()
            value = value.strip()
            if not key:
                continue
            if value in {"0", "1"}:
                parsed[key] = value == "1"
            else:
                parsed[key] = value
        if not parsed:
            return False
        self._latest_status = {
            "connected": True,
            "device": self._connected_path,
            "status": parsed,
            "raw": line,
            "notes": [],
        }
        return True

    def _mark_error_locked(self, context: str, exc: Exception, keep_connected: bool = False):
        self._log_throttled(context, logging.WARNING, "Hardware controller %s: %s", context, exc)
        if not keep_connected:
            self._close_locked()
        self._latest_status = {
            "connected": keep_connected and self._serial is not None,
            "device": self._connected_path if keep_connected else None,
            "status": dict(self._latest_status.get("status") or {}),
            "raw": self._latest_status.get("raw"),
            "notes": [f"{context}: {exc}"],
        }

    def _close_locked(self):
        ser = self._serial
        self._serial = None
        self._connected_path = None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def _log_throttled(self, key: str, level: int, message: str, *args):
        now = time.monotonic()
        if now - self._last_log.get(key, 0.0) < 30.0:
            return
        self._last_log[key] = now
        logger.log(level, message, *args)
