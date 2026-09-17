# SPDX-License-Identifier: AGPL-3.0-only

"""Invoke-time device validation for committed-plan release adapters.

A release adapter pins its device context (output key, channel count,
discovered playback ports) when a job commits. The session release may run
later, after an idle-window device change; rebuilding the pinned graph over
the live selection would then address the wrong device. Both adapters
(AutoSub, Speaker Align) therefore resolve the live selection at invoke time
through an injected resolver and share this pure primitive, so both refuse
identically and fail closed before touching the runtime graph.
"""

from __future__ import annotations

from typing import Any


def check_release_device(
    *,
    output_key: str,
    channels: int,
    hardware_ports: list,
    live: Any,
    owner: str,
) -> None:
    """Refuse when the live selection no longer matches the pinned context.

    Returns None on an exact match. Raises RuntimeError naming every
    mismatched field — output key, channel count, playback ports in that
    order — when the live device moved, and also when the live selection is
    missing or malformed (unresolvable counts as moved: rebuilding blind is
    never acceptable). Port comparison is exact, including order, because
    the pinned order drives the channel mapping.
    """
    problems: list[str] = []
    live_key: Any = None
    live_channels: Any = None
    live_ports: Any = None
    if isinstance(live, dict):
        live_key = live.get("output_key")
        live_channels = live.get("channels")
        live_ports = live.get("hardware_ports")
    if not (isinstance(live_key, str) and live_key and live_key == output_key):
        problems.append(
            f"output_key (pinned {output_key!r} vs live {live_key!r})")
    if type(live_channels) is not int or live_channels != channels:
        problems.append(
            f"channels (pinned {channels!r} vs live {live_channels!r})")
    if not (isinstance(live_ports, list) and live_ports == list(hardware_ports)):
        problems.append(
            f"hardware_ports (pinned {list(hardware_ports)!r} vs live {live_ports!r})")
    if problems:
        raise RuntimeError(
            f"{owner} committed release device changed since commit: "
            + "; ".join(problems)
            + "; refusing pinned rebuild")
