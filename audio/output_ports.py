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

import re
from typing import Any, Iterable, Mapping

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
    historic semantic port list used when discovery could not provide one, so
    a diagnostic never invents a topology that was not actually resolved.
    """
    discovered = (output_mode or {}).get("hardware_playback_ports")
    ports = tuple(str(port) for port in discovered) if isinstance(discovered, (list, tuple)) else ()
    if not ports:
        ports = tuple(str(port) for port in fallback if port)
    return ports[:max(0, count)]
