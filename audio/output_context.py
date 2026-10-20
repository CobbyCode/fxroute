# SPDX-License-Identifier: AGPL-3.0-only
"""Resolve the selected output device and its effective state topology."""

from fastapi import HTTPException

from audio.output_state import effective_crossover_enabled, routing_for_device
from audio.output_topology import derive_topology


def output_state_device(overview: dict) -> tuple[str, int | None]:
    mode = overview.get("output_mode") or {}
    selected = overview.get("selected_output") or {}
    key = str(mode.get("effective_output_key") or selected.get("key") or "")
    channels = mode.get("effective_output_channels", selected.get("channels"))
    if not key:
        raise HTTPException(status_code=400, detail="No audio output device is selected")
    if channels is None:
        return key, None
    try:
        count = int(channels)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Output channel capacity is unknown")
    if count < 0:
        raise HTTPException(status_code=400, detail="Output channel capacity is unknown")
    return key, count


def output_state_topology(state: dict, mode: str, output_key: str, channels: int | None) -> dict:
    topology = derive_topology(mode, routing_for_device(state, mode, output_key, channels), channels=channels,
                               crossover_enabled=effective_crossover_enabled(state, mode, channels, output_key=output_key))
    return {"mode": topology.mode, "crossover_enabled": topology.crossover_enabled, "roles": list(topology.roles),
            "sub_roles": list(topology.sub_roles), "sub_mode": topology.sub_mode,
            "left_ways": list(topology.left_ways), "right_ways": list(topology.right_ways),
            "center_ways": list(topology.center_ways), "center_way_count": topology.center_way_count,
            "way_count": topology.way_count, "issues": list(topology.issues)}
