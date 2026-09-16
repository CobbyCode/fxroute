# SPDX-License-Identifier: AGPL-3.0-only
"""Compile role/bank state into a backend-independent processing plan.

This is not native engine configuration. Native output-bank execution and
coefficient/IR compilation must consume this plan before it can be activated.
"""

from __future__ import annotations

import copy
from collections.abc import Callable

from audio.output_routing import device_key
from audio.output_state import routing_for_device, validate_output_state
from audio.output_topology import SUB_ROLES, derive_topology
from audio.samplerate.constants import FXROUTE_MAX_PROCESSING_RATE
from dsp.banks import BankState


def _bank_plan(bank_id: str, payload: dict, preset_loader: Callable[[str], dict]) -> dict:
    bank = BankState.from_dict(payload)
    preset = preset_loader(bank.preset)
    chain = copy.deepcopy(preset["chain"])
    if bank_id != "global":
        if any(plugin["type"] not in {"equalizer", "convolver"}
               for plugin in chain if plugin.get("enabled", True)):
            raise ValueError(f"Area bank {bank_id} supports only PEQ and convolver presets")
    bypass = bank.preset == "Direct"
    return {"bank_id": bank_id, "preset": bank.preset,
            "bypass": bypass, "chain": [] if bypass else chain}


def _input_routes(role: str, sub_mode: str) -> list[dict]:
    if role in SUB_ROLES and sub_mode != "stereo":
        return [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}]
    left = role in {"main_l", "sub_l"} or role.startswith("left_")
    return [{"input": 0 if left else 1, "gain": 1.0}]


def _crossover_filters(mode: str, role: str, processing: dict, bass: dict, has_subs: bool) -> list[dict]:
    if mode == "crossover" and role not in SUB_ROLES:
        way = role.split("_", 1)[1]
        required = ("lowpass",) if way == "low" else ("highpass",) if way == "high" else ("highpass", "lowpass")
        if any(processing[kind] is None for kind in required):
            raise ValueError(f"Crossover way {role} requires {' and '.join(required)}")
    filters = [{"kind": kind, **processing[kind]} for kind in ("highpass", "lowpass") if processing[kind] is not None]
    if has_subs and (role in SUB_ROLES or bass["main_highpass_enabled"]):
        filters.append({"kind": "lowpass" if role in SUB_ROLES else "highpass",
                        "family": "linkwitz-riley", "slope_db_oct": 24,
                        "frequency_hz": bass["frequency_hz"]})
    return filters


def compile_processing_plan(state: dict, *, output_key: str, channels: int,
                            sample_rate_hz: int, preset_loader: Callable[[str], dict]) -> dict:
    """Resolve banks using a validating loader such as DSPPresetStore.read."""
    state = validate_output_state(state)
    if type(sample_rate_hz) is not int or not 0 < sample_rate_hz <= FXROUTE_MAX_PROCESSING_RATE:
        raise ValueError("Processing sample rate must be a positive integer up to 384000")
    mode = state["active_mode"]
    config = state["modes"][mode]
    assignments = routing_for_device(state, mode, output_key)
    topology = derive_topology(mode, assignments, channels=channels)
    topology.require_activatable()
    processing = config["processing"]
    delay_offset = max(0.0, -min(processing[role]["alignment_ms"] for role in topology.roles))
    global_bank = _bank_plan("global", config["banks"]["global"], preset_loader)
    global_bank["extras"] = {} if global_bank["bypass"] else copy.deepcopy(config["extras"])
    outputs = []
    for role in topology.roles:
        settings = processing[role]
        filters = _crossover_filters(mode, role, settings, config["bass_management"], bool(topology.sub_roles))
        if any(item["frequency_hz"] >= sample_rate_hz / 2 for item in filters):
            raise ValueError(f"Crossover frequency for {role} must be below Nyquist")
        outputs.append({
            "role": role, "routes": _input_routes(role, topology.sub_mode),
            "crossover": filters,
            "bank": _bank_plan(role, config["banks"][role], preset_loader),
            "gain_db": settings["level_db"],
            "delay_ms": settings["alignment_ms"] + delay_offset,
            "invert": settings["polarity"] == "invert",
        })
    indices = {role: index for index, role in enumerate(topology.roles)}
    edges = [{"output": indices[role], "channel": index}
             for index, role in enumerate(assignments[:channels]) if role != "off"]
    return {
        "schema": "fxroute.dsp.processing-plan", "version": 1,
        "mode": mode, "device_key": device_key(output_key), "sample_rate_hz": sample_rate_hz,
        "sub_mode": topology.sub_mode, "way_count": topology.way_count,
        "order": ["global", "matrix", "crossover", "area-bank", "output-trim", "guard-mute"],
        "global": global_bank, "outputs": outputs, "physical_routes": edges,
    }
