# SPDX-License-Identifier: AGPL-3.0-only
"""Shared raw-PCM contract for the local pipe client and receiver."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from audio.output_topology import SURROUND_POSITIONS

FORMAT_WIDTHS = {"s16le": 2, "s24le": 3, "s32le": 4, "f32le": 4}
MAX_HEADER_BYTES = 4096
CHUNK_BYTES = 64 * 1024

# Channel-map entry that leaves a PCM channel unused.
UNUSED_CHANNEL = "-"
# Spellings of the PipeWire/ALSA names; FXRoute calls the rear pair BL/BR.
POSITION_ALIASES = {"RL": "BL", "RR": "BR"}
# Program positions in the order a channel map is written (WAV/ALSA order).
MAP_ORDER = ("FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR")
# Layout of a stream with this many channels when no map or pair is given.
DEFAULT_LAYOUTS = {
    4: ("FL", "FR", "BL", "BR"),
    6: ("FL", "FR", "FC", "LFE", "BL", "BR"),
    8: MAP_ORDER,
}


@dataclass(frozen=True)
class PcmSpec:
    format: str
    rate: int
    channels: int
    left: int
    right: int
    # Program position of every PCM channel, UNUSED_CHANNEL for a skipped one;
    # empty for a plain left/right pair.
    channel_map: tuple[str, ...] = ()

    @property
    def frame_bytes(self) -> int:
        return FORMAT_WIDTHS[self.format] * self.channels

    @property
    def routes(self) -> tuple[tuple[str, int], ...]:
        """(program position, 1-based PCM channel) pairs in engine input order."""
        if not self.channel_map:
            return (("FL", self.left), ("FR", self.right))
        by_position = {position: index + 1 for index, position in enumerate(self.channel_map)
                       if position != UNUSED_CHANNEL}
        return tuple((position, by_position[position])
                     for position in SURROUND_POSITIONS if position in by_position)

    @property
    def multichannel(self) -> bool:
        """Whether the layout feeds a program position beyond the front pair."""
        return any(position not in ("FL", "FR") for position, _ in self.routes)

    def header(self) -> dict[str, object]:
        header: dict[str, object] = {"version": 1, "format": self.format, "rate": self.rate,
                                     "channels": self.channels}
        if self.channel_map:
            header["channel_map"] = list(self.channel_map)
        else:
            header.update(left=self.left, right=self.right)
        return header


def parse_channel_map(value: object, channels: int) -> tuple[str, ...]:
    """Validate a channel map (comma string or list) against the channel count.

    Entries name the program position of PCM channel 1, 2, ... and may stop
    before ``channels``; the channels after the last entry stay unused.
    """
    entries = value.split(",") if isinstance(value, str) else value
    if not isinstance(entries, (list, tuple)) or not entries:
        raise ValueError("Channel map must list one position per PCM channel")
    if len(entries) > channels:
        raise ValueError(f"Channel map names {len(entries)} channels but the PCM stream has {channels}")
    mapped: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            raise ValueError("Channel map entries must be position names")
        name = entry.strip().upper()
        name = POSITION_ALIASES.get(name, name)
        if name != UNUSED_CHANNEL and name not in SURROUND_POSITIONS:
            raise ValueError(f"Unknown channel position {entry.strip()!r}; use FL, FR, FC, LFE, RL, RR, SL, SR or -")
        if name != UNUSED_CHANNEL and name in mapped:
            raise ValueError(f"Channel map lists {name} more than once")
        mapped.append(name)
    if "FL" not in mapped or "FR" not in mapped:
        raise ValueError("Channel map must include FL and FR")
    return tuple(mapped) + (UNUSED_CHANNEL,) * (channels - len(mapped))


def parse_spec(payload: object) -> PcmSpec:
    if not isinstance(payload, dict) or set(payload) - {
        "version", "format", "rate", "channels", "left", "right", "channel_map"
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
    pair_given = "left" in payload or "right" in payload
    if "channel_map" in payload:
        if pair_given:
            raise ValueError("Use either --channel-map or --left/--right, not both")
        channel_map = parse_channel_map(payload["channel_map"], channels)
        return PcmSpec(format_name, rate, channels, channel_map.index("FL") + 1,
                       channel_map.index("FR") + 1, channel_map)
    if not pair_given and channels <= 2:
        return PcmSpec(format_name, rate, channels, 1, channels)
    if not pair_given and channels in DEFAULT_LAYOUTS:
        channel_map = parse_channel_map(DEFAULT_LAYOUTS[channels], channels)
        return PcmSpec(format_name, rate, channels, 1, 2, channel_map)
    left, right = payload.get("left"), payload.get("right")
    if any(type(value) is not int or not 1 <= value <= channels for value in (left, right)):
        raise ValueError("Specify --channel-map, or --left and --right within the PCM channel count")
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
