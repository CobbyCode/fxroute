# SPDX-License-Identifier: AGPL-3.0-only
"""Shared raw-PCM contract for the local pipe client and receiver."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

FORMAT_WIDTHS = {"s16le": 2, "s24le": 3, "s32le": 4, "f32le": 4}
MAX_HEADER_BYTES = 4096
CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class PcmSpec:
    format: str
    rate: int
    channels: int
    left: int
    right: int

    @property
    def frame_bytes(self) -> int:
        return FORMAT_WIDTHS[self.format] * self.channels

    def header(self) -> dict[str, object]:
        return {"version": 1, "format": self.format, "rate": self.rate,
                "channels": self.channels, "left": self.left, "right": self.right}


def parse_spec(payload: object) -> PcmSpec:
    if not isinstance(payload, dict) or set(payload) - {
        "version", "format", "rate", "channels", "left", "right"
    }:
        raise ValueError("Invalid PCM metadata fields")
    if type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("Unsupported PCM protocol version")
    format_name = payload.get("format")
    if not isinstance(format_name, str) or format_name not in FORMAT_WIDTHS:
        raise ValueError("PCM format must be s16le, s24le, s32le or f32le")
    rate, channels = payload.get("rate"), payload.get("channels")
    if type(rate) is not int or not 8000 <= rate <= 384000:
        raise ValueError("PCM rate must be an integer from 8000 to 384000 Hz")
    if type(channels) is not int or not 1 <= channels <= 32:
        raise ValueError("PCM channel count must be an integer from 1 to 32")
    if "left" not in payload and "right" not in payload and channels <= 2:
        left, right = 1, channels
    else:
        left, right = payload.get("left"), payload.get("right")
    if any(type(value) is not int or not 1 <= value <= channels for value in (left, right)):
        raise ValueError("Specify --left and --right within the PCM channel count")
    if channels > 1 and left == right:
        raise ValueError("Left and right must select different PCM channels")
    return PcmSpec(format_name, rate, channels, left, right)


def encode_event(event: str, **fields: object) -> bytes:
    return (json.dumps({"event": event, **fields}, allow_nan=False,
                       separators=(",", ":")) + "\n").encode()


def socket_path(override: str | None = None) -> Path:
    path = Path(override) if override else Path(
        os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    ) / "fxroute" / "stdin.sock"
    if not path.is_absolute():
        raise ValueError("STDIN socket path must be absolute")
    return path


class FrameAssembler:
    def __init__(self, frame_bytes: int):
        if frame_bytes < 1:
            raise ValueError("Invalid PCM frame size")
        self.frame_bytes = frame_bytes
        self._tail = b""

    def feed(self, data: bytes) -> bytes:
        combined = self._tail + data
        end = len(combined) - len(combined) % self.frame_bytes
        self._tail = combined[end:]
        return combined[:end]

    def finish(self) -> None:
        if self._tail:
            raise ValueError("Incomplete PCM frame at EOF")
