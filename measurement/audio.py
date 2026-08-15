"""Low-level PipeWire command ownership for measurement capture."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any


class MeasurementAudioAdapter:
    """Own bounded PipeWire node, port, and link operations."""

    def __init__(self, *, command_runner: Callable[..., Any] | None = None) -> None:
        self._run = command_runner or subprocess.run

    def list_pw_ports(self, node_name: str) -> list[str]:
        try:
            completed = self._run(["pw-link", "-io"], capture_output=True, text=True, timeout=3)
        except Exception:
            return []
        if completed.returncode != 0:
            return []
        prefix = f"{node_name}:"
        return [line.strip() for line in (completed.stdout or "").splitlines() if line.strip().startswith(prefix)]

    def create_pipewire_link(self, source_port: str, target_port: str) -> None:
        try:
            self._run(
                ["pw-link", source_port, target_port],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "").strip()
            if "already exists" not in message.lower() and "file exists" not in message.lower():
                raise RuntimeError(
                    f"Could not create measurement playback link ({source_port} -> {target_port}): "
                    f"{message or exc}"
                ) from exc

    def disconnect_link(self, source_port: str, target_port: str) -> bool:
        try:
            result = self._run(
                ["pw-link", "-d", source_port, target_port],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                return True
            output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
            return any(phrase in output for phrase in (
                "not found", "no such", "cannot find", "link not found", "does not exist", "no link",
            ))
        except Exception:
            return False

    def supports_option(self, option: str) -> bool:
        try:
            completed = self._run(["pw-record", "--help"], capture_output=True, text=True, timeout=3)
        except Exception:
            return False
        help_text = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        return completed.returncode == 0 and option in help_text
