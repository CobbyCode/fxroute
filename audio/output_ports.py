# SPDX-License-Identifier: AGPL-3.0-only
"""Resolution of the selected hardware sink's actual PipeWire playback ports.

PipeWire names a sink's playback ports after the device.  Interfaces that
carry a channel map expose the semantic names ``playback_FL``, ``playback_FR``,
``playback_RL`` …; multichannel interfaces without one (for example USB
interfaces that publish raw channels) expose them as ``playback_AUX0`` …
``playback_AUX17`` instead.  FXRoute's DSP outputs are fixed (Out 1/2 Main,
Out 3/4 Sub), so the runtime has to map its logical outputs onto whatever
playback ports the selected sink actually publishes, in the device's own
channel order — never onto a hardcoded port-name topology.

Every consumer (DSP link build, graph diagnosis, link watcher/repair and
readback) resolves through the functions in this module, so one device can
never be described by two different port lists.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

# Rate limit for the semantic-fallback warning: the graph sync loop resolves
# ports far more often than a user reads logs, so the state is announced once
# per output key and re-announced after this interval.
SEMANTIC_FALLBACK_WARN_INTERVAL_S = 300.0
_warn_lock = threading.Lock()
_semantic_fallback_warned_at: dict[str, float] = {}

# The conventional hardware channel order FXRoute assigns to its DSP outputs:
# Out 1 = Main L, Out 2 = Main R, Out 3 = Sub 1, Out 4 = Sub 2.
HARDWARE_CHANNEL_ORDER = ("FL", "FR", "RL", "RR")
PLAYBACK_PREFIX = "playback_"


def natural_sort_key(name: str) -> list[tuple[int, Any]]:
    """Sort key that orders ``AUX0, AUX1, AUX2, … AUX10`` by channel number.

    A lexicographic sort would produce ``AUX0, AUX1, AUX10, AUX11, AUX2``,
    which silently swaps the physical channels of a multichannel device.
    Numeric fragments compare as numbers, text fragments case-insensitively;
    the leading tag keeps the two types from ever being compared directly.
    """
    return [
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", name)
    ]


def warn_semantic_playback_fallback(output_key: str) -> None:
    """Announce (rate-limited) that a sink's ports could not be resolved.

    The discovery payload, the direct ``pw-link -io`` fallback *and* the
    sink's own channel map failed to describe ``output_key``, so the consumer
    substituted the historic semantic port names (``playback_FL/FR/…``).  On a
    device that really exposes only raw channels (``playback_AUX0…``) that
    topology can never link, and without this warning the field symptom is
    only a stuck playback transition.  The previous fallback behavior is
    unchanged; this only makes the rare path visible in the logs.
    """
    key = str(output_key or "the selected output")
    now = time.monotonic()
    with _warn_lock:
        last = _semantic_fallback_warned_at.get(key, 0.0)
        if now - last < SEMANTIC_FALLBACK_WARN_INTERVAL_S:
            return
        _semantic_fallback_warned_at[key] = now
    logger.warning(
        "Hardware playback ports for %s could not be resolved "
        "(no output-discovery payload, 'pw-link -io' unavailable or no "
        "playback_ ports listed, and no usable channel map). Falling back to "
        "semantic port names playback_FL/FR(/RL/RR); linking will fail on "
        "devices that expose only raw channels (playback_AUX0…). Check the "
        "PipeWire port list and the selected output device.",
        key,
    )


# PipeWire channel designations (``audio.channel`` / ``Channel Map``) mapped
# to the port-name suffix PipeWire publishes in both directions
# (``playback_FL`` / ``capture_FL``, ``playback_AUX0`` / ``capture_AUX0``).  A
# node's own channel map always beats positional guessing by channel count;
# the positional table is only the fallback for a node that reports no usable
# designations.  Capture (external inputs) and playback (hardware sink ports)
# share this table so one device is never described two ways.
PIPEWIRE_CHANNEL_SUFFIX_BY_POSITION = {
    "front-left": "FL",
    "front-right": "FR",
    "rear-left": "RL",
    "rear-right": "RR",
    "front-center": "FC",
    "lfe": "LFE",
    "side-left": "SL",
    "side-right": "SR",
    "mono": "MONO",
}

POSITIONAL_CHANNEL_SUFFIXES = (
    "FL", "FR", "RL", "RR", "FC", "LFE", "SL", "SR",
    "AUX0", "AUX1", "AUX2", "AUX3", "AUX4", "AUX5",
)


def parse_channel_map_positions(channel_map: str | Iterable[str] | None) -> list[str]:
    """Split a ``Channel Map`` into its lower-cased channel designations."""
    if channel_map is None:
        return []
    raw_tokens = channel_map.split(",") if isinstance(channel_map, str) else channel_map
    tokens: list[str] = []
    for raw_token in raw_tokens:
        token = str(raw_token).strip().lower()
        if token:
            tokens.append(token)
    return tokens


def channel_suffix_for_position(token: str, index: int) -> str:
    """Port-name suffix for one channel designation at a zero-based position."""
    mapped = PIPEWIRE_CHANNEL_SUFFIX_BY_POSITION.get(token)
    if mapped:
        return mapped
    aux_match = re.match(r"^aux(\d+)$", token)
    if aux_match:
        return f"AUX{int(aux_match.group(1))}"
    if 0 <= index < len(POSITIONAL_CHANNEL_SUFFIXES):
        return POSITIONAL_CHANNEL_SUFFIXES[index]
    return f"AUX{index}"


def playback_ports_from_channel_map(
    channel_map: str | Iterable[str] | None,
    channels: int | None,
) -> tuple[str, ...]:
    """Derive a sink's ordered playback ports from its own channel map.

    ``pactl list sinks`` publishes ``Channel Map:`` for every sink even while
    the node has not published its ports yet (a suspended device), and
    PipeWire names each port after exactly those designations.  Deriving the
    list therefore yields the device's real topology — ``playback_AUX0…`` for
    a raw-channel interface, ``playback_FL/FR…`` for a mapped one — instead of
    guessing the semantic names, which can never link on the former.  This is
    the fallback when ``pw-link -io`` lists no playback ports; it is not a
    substitute for a real listing.
    """
    positions = parse_channel_map_positions(channel_map)
    if not positions:
        # No designations at all: inventing a positional order here would
        # produce precisely the FL/FR guess this fallback exists to replace.
        return ()
    suffixes = [
        channel_suffix_for_position(token, index)
        for index, token in enumerate(positions)
    ]
    if channels is not None:
        try:
            count = int(channels)
        except (TypeError, ValueError):
            count = None
        if count is not None:
            if count <= 0:
                return ()
            if len(suffixes) < count:
                suffixes.extend(
                    channel_suffix_for_position("", index)
                    for index in range(len(suffixes), count)
                )
            suffixes = suffixes[:count]
    return tuple(f"{PLAYBACK_PREFIX}{suffix}" for suffix in suffixes if suffix)


def playback_port_names(io_text: str, node_name: str) -> tuple[str, ...]:
    """Return ``node_name``'s playback ports as named in a ``pw-link`` listing."""
    if not io_text or not node_name:
        return ()
    prefix = f"{node_name}:"
    ports: list[str] = []
    for raw in io_text.splitlines():
        line = raw.strip()
        if not line.startswith(prefix):
            continue
        port = line[len(prefix):]
        if port.startswith(PLAYBACK_PREFIX) and port not in ports:
            ports.append(port)
    return tuple(ports)


def order_hardware_playback_ports(ports: Iterable[str]) -> tuple[str, ...]:
    """Order playback ports semantically first, then in natural channel order.

    ``FL/FR/RL/RR`` keep their conventional meaning when the device exposes
    them.  Every other port (``AUX0``, ``AUX1``, …) follows in channel order,
    so ``AUX10`` never sorts before ``AUX2``.
    """
    unique: list[str] = []
    for port in ports:
        if port and port not in unique:
            unique.append(port)
    semantic: dict[str, str] = {}
    remaining: list[str] = []
    for port in unique:
        channel = port[len(PLAYBACK_PREFIX):] if port.startswith(PLAYBACK_PREFIX) else port
        if channel in HARDWARE_CHANNEL_ORDER and channel not in semantic:
            semantic[channel] = port
        else:
            remaining.append(port)
    ordered = [semantic[channel] for channel in HARDWARE_CHANNEL_ORDER if channel in semantic]
    ordered.extend(sorted(remaining, key=natural_sort_key))
    return tuple(ordered)


def resolve_hardware_playback_ports(io_text: str, output_key: str) -> tuple[str, ...]:
    """Resolve the selected sink's ordered playback ports from ``pw-link -io``."""
    return order_hardware_playback_ports(playback_port_names(io_text, str(output_key or "").strip()))


def hardware_playback_ports_from_mode(
    output_mode: Mapping[str, Any] | None,
    fallback: Iterable[str],
    *,
    count: int,
) -> tuple[str, ...]:
    """Return the mode's resolved hardware playback ports, at most ``count``.

    ``output_mode`` is the discovery payload, which carries the already
    resolved list under ``hardware_playback_ports``.  ``fallback`` is the
    consumer's own list for the case where the payload carries none (a live
    read, the channel-map-derived ports, or finally the historic semantic
    names), so a diagnostic never invents a topology that was not resolved.
    """
    discovered = (output_mode or {}).get("hardware_playback_ports")
    ports = tuple(str(port) for port in discovered) if isinstance(discovered, (list, tuple)) else ()
    if not ports:
        ports = tuple(str(port) for port in fallback if port)
    return ports[:max(0, count)]


def hardware_playback_port_fallback_from_mode(
    output_mode: Mapping[str, Any] | None,
    *,
    count: int | None = None,
) -> tuple[str, ...]:
    """The channel-map-derived playback ports carried by the discovery payload.

    Empty when discovery could not read the sink's channel map either.  A
    consumer uses this between its live ``pw-link -io`` read and the historic
    semantic names, so an AUX-only device that is suspended (its ports are not
    published yet) still links against its real ``playback_AUX0…`` topology.
    ``count`` truncates like the other accessors; ``None`` keeps the full order
    for callers that map their own fixed outputs onto it.
    """
    derived = (output_mode or {}).get("hardware_playback_ports_from_channel_map")
    ports = tuple(str(port) for port in derived) if isinstance(derived, (list, tuple)) else ()
    return ports[:max(0, count)] if count is not None else ports
