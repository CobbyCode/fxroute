# SPDX-License-Identifier: AGPL-3.0-only
"""Compile role/bank state into a backend-independent processing plan.

This is not native engine configuration. Native output-bank execution and
coefficient/IR compilation must consume this plan before it can be activated.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable

from audio.output_routing import device_key
from audio.output_state import (bass_crossover_for_group, bass_crossover_for_side,
                                effective_crossover_enabled, routing_for_device,
                                shared_bass_crossover, validate_output_state)
from audio.output_topology import (MAIN_ROLES, SUB_ROLES, SURROUND_POSITIONS, derive_topology,
                                   side_for_role, surround_group)
from audio.samplerate.constants import FXROUTE_MAX_PROCESSING_RATE
from dsp.banks import BankState
from dsp.surround_global import (BUILTIN_PRESETS, LAYOUT as SURROUND_LAYOUT, preset_layout,
                                 routed_chain, routed_groups, validate_surround_chain)

logger = logging.getLogger(__name__)


def _bank_plan(bank_id: str, payload: dict, preset_loader: Callable[[str], dict],
                *, neutralized: bool = False, surround_groups: frozenset[str] | None = None) -> dict:
    """Plan one bank; ``surround_groups`` marks the Surround Global bank.

    Its chain keeps only plugins of speaker groups with a routed speaker. A
    stored preset without the Surround layout cannot be assigned any more and
    renders as Neutral instead of blocking the activation.
    """
    if neutralized:
        return {"bank_id": bank_id, "preset": "Direct",
                "bypass": True, "chain": []}
    bank = BankState.from_dict(payload)
    preset = preset_loader(bank.preset)
    chain = copy.deepcopy(preset["chain"])
    name = bank.preset
    if surround_groups is not None and name not in BUILTIN_PRESETS:
        if preset_layout(preset) != SURROUND_LAYOUT:
            logger.warning('Surround Global preset "%s" has no Surround layout; rendering Neutral', name)
            name, chain = "Neutral", []
        else:
            validate_surround_chain(chain, preset=name)
            chain = routed_chain(chain, surround_groups)
    elif bank_id != "global":
        if any(plugin["type"] not in {"equalizer", "convolver"}
               for plugin in chain if plugin.get("enabled", True)):
            raise ValueError(f"Area bank {bank_id} supports only PEQ and convolver presets")
    bypass = name == "Direct"
    return {"bank_id": bank_id, "preset": name,
            "bypass": bypass, "chain": [] if bypass else chain}


# -3 dB fold-down, FFmpeg's default center and surround mix level.
FOLD_GAIN = 0.7071067811865476
_SPEAKER_POSITIONS = {"main_l": 0, "main_r": 1, "center": 2,
                      "side_l": 4, "side_r": 5, "rear_l": 6, "rear_r": 7}


def _surround_speaker(role: str) -> str:
    """Speaker position a role reproduces; crossover ways play their front side."""
    if role.startswith("left_"):
        return "main_l"
    if role.startswith("right_"):
        return "main_r"
    return role


def surround_destinations(roles) -> dict[int, list[tuple[str, float]]]:
    """Speakers reproducing each program position (LFE excluded).

    A position without its own speaker folds down instead of going silent:
    Center to Front L/R at -3 dB each, Side and Rear to the other surround
    pair, and to Front L/R at -3 dB when neither pair is routed.
    """
    present = {_surround_speaker(role) for role in roles if role not in SUB_ROLES}
    fronts = [("main_l", FOLD_GAIN), ("main_r", FOLD_GAIN)]

    def surround(own: str, other: str, front: str) -> list[tuple[str, float]]:
        if own in present:
            return [(own, 1.0)]
        if other in present:
            return [(other, 1.0)]
        return [(front, FOLD_GAIN)]

    return {0: [("main_l", 1.0)], 1: [("main_r", 1.0)],
            2: [("center", 1.0)] if "center" in present else fronts,
            4: surround("side_l", "rear_l", "main_l"), 5: surround("side_r", "rear_r", "main_r"),
            6: surround("rear_l", "side_l", "main_l"), 7: surround("rear_r", "side_r", "main_r")}


def _surround_routes(role: str, destinations: dict, *, has_subs: bool) -> list[dict]:
    """Own position first, then fold-down sends; measurement excites only the first."""
    speaker = _surround_speaker(role)
    own = _SPEAKER_POSITIONS[speaker]
    routes = [{"input": own, "gain": 1.0}]
    routes.extend({"input": position, "gain": gain} for position, targets in sorted(destinations.items())
                  if position != own for target, gain in targets if target == speaker)
    if speaker in MAIN_ROLES and not has_subs:
        routes.append({"input": 3, "gain": 0.5})
    return routes


def _surround_sub_bass(bass: dict, destinations: dict) -> tuple[list[dict], list[dict]]:
    """Common mono bass bus, low-passed per speaker group that plays each position.

    Sends sharing the Front crossover form the primary bus; every other
    distinct group crossover gets its own low-passed bus. Linked groups
    therefore keep one shared bus.
    """
    primary = bass_crossover_for_group(bass, "front")[0]
    routes, groups = [], {}
    for position, targets in sorted(destinations.items()):
        crossover = bass_crossover_for_group(bass, surround_group(targets[0][0]))[0]
        send = {"input": position, "gain": 0.5}
        if crossover == primary:
            routes.append(send)
        else:
            groups.setdefault(tuple(sorted(crossover.items())), (crossover, []))[1].append(send)
    return routes, [{"crossover": {"kind": "lowpass", **crossover}, "routes": sends}
                    for crossover, sends in groups.values()]


def _input_routes(role: str, sub_mode: str) -> list[dict]:
    if role in SUB_ROLES and sub_mode != "stereo":
        return [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}]
    left = role in {"main_l", "sub_l"} or role.startswith("left_")
    return [{"input": 0 if left else 1, "gain": 1.0}]


def _bass_crossover(bass: dict, role: str, sub_mode: str) -> dict:
    """Range the sub crossover filter for one role.

    Only a true Left/Right sub pair resolves per side; Mono and Dual-Mono
    always run the shared crossover, so their two subs cannot drift apart.
    """
    if sub_mode != "stereo":
        return shared_bass_crossover(bass)
    return bass_crossover_for_side(bass, side_for_role(role))


def _crossover_filters(role: str, processing: dict, bass: dict,
                       has_subs: bool, sub_mode: str, *, surround: bool = False) -> list[dict]:
    """Crossover filters of one role; a cleared direction stays absent.

    Off is a valid operating state: a way whose filter was cleared simply
    runs without a filter in that direction instead of making the whole
    plan uncompilable. Cleared directions are reported by the crossover
    response so the UI can show them as deliberate, not as an error.
    """
    filters = [{"kind": kind, **processing[kind]} for kind in ("highpass", "lowpass") if processing[kind] is not None]
    if surround and has_subs:
        # Each speaker group high-passes at its own crossover; the subs list
        # the Front group's low-pass, other groups ride separate bass buses.
        group = "front" if role in SUB_ROLES else surround_group(role)
        crossover, highpass = bass_crossover_for_group(bass, group)
        if role in SUB_ROLES or highpass:
            filters.append({"kind": "lowpass" if role in SUB_ROLES else "highpass", **crossover})
        return filters
    if has_subs and (role in SUB_ROLES or bass["main_highpass_enabled"]):
        # The sub low-pass and the Main high-pass it implies share the mode's
        # crossover filter, type and slope included.
        filters.append({"kind": "lowpass" if role in SUB_ROLES else "highpass",
                        **_bass_crossover(bass, role, sub_mode)})
    return filters


def _rendered_crossover(config: dict, topology) -> dict[str, list[dict]]:
    return {role: _crossover_filters(role, config["processing"][role], config["bass_management"],
                                     bool(topology.sub_roles), topology.sub_mode,
                                     surround=topology.mode == "surround")
            for role in topology.roles}


def rendered_crossover_filters(state: dict, *, output_key: str, channels: int) -> dict[str, list[dict]]:
    """Crossover filters every active role renders, keyed by role.

    The same list ``compile_processing_plan`` puts into each output: the way's
    own highpass/lowpass plus, with subs routed, the bass-management filter the
    role inherits (sub low-pass, Main high-pass). A model of a way's rendered
    response has to read it here: the role's own processing alone misses the
    Main high-pass.
    """
    state = validate_output_state(state)
    mode = state["active_mode"]
    config = state["modes"][mode]
    xo_enabled = effective_crossover_enabled(state, mode, channels)
    topology = derive_topology(mode, routing_for_device(state, mode, output_key, channels), channels=channels,
                               crossover_enabled=xo_enabled)
    topology.require_activatable()
    return copy.deepcopy(_rendered_crossover(config, topology))


def compile_processing_plan(state: dict, *, output_key: str, channels: int,
                            sample_rate_hz: int, preset_loader: Callable[[str], dict],
                            neutralize_banks: bool = False) -> dict:
    """Resolve banks using a validating loader such as DSPPresetStore.read.

    With neutralize_banks, Global and area PEQ/convolver banks render
    bypassed (Direct, empty chain, no extras) while crossover filters,
    output trims (gain/delay/polarity), routing and bass stay active.
    This is the shared alignment measurement rendering: Delay/Gain as
    physical base tuning is measured through the active crossover, before
    any PEQ/convolver correction.
    """
    state = validate_output_state(state)
    if type(sample_rate_hz) is not int or not 0 < sample_rate_hz <= FXROUTE_MAX_PROCESSING_RATE:
        raise ValueError("Processing sample rate must be a positive integer up to 384000")
    if type(neutralize_banks) is not bool:
        raise ValueError("neutralize_banks must be a boolean")
    mode = state["active_mode"]
    config = state["modes"][mode]
    xo_enabled = effective_crossover_enabled(state, mode, channels)
    assignments = routing_for_device(state, mode, output_key, channels)
    topology = derive_topology(mode, assignments, channels=channels, crossover_enabled=xo_enabled)
    topology.require_activatable()
    processing = config["processing"]
    delay_offset = max(0.0, -min(processing[role]["alignment_ms"] for role in topology.roles))
    # Surround Global holds group-tagged PEQ/convolver plugins; groups without
    # a routed speaker render nothing (see dsp.surround_global).
    surround_groups = routed_groups(topology.roles) if mode == "surround" else None
    global_bank = _bank_plan("global", config["banks"]["global"], preset_loader,
                             neutralized=neutralize_banks, surround_groups=surround_groups)
    global_bank["extras"] = {} if (global_bank["bypass"] or neutralize_banks) else copy.deepcopy(config["extras"])
    crossover = _rendered_crossover(config, topology)
    surround = mode == "surround"
    destinations = surround_destinations(topology.roles) if surround else {}
    has_subs = bool(topology.sub_roles)
    outputs = []
    for role in topology.roles:
        settings = processing[role]
        filters = crossover[role]
        if any(item["frequency_hz"] >= sample_rate_hz / 2 for item in filters):
            raise ValueError(f"Crossover frequency for {role} must be below Nyquist")
        if surround and role in SUB_ROLES:
            routes, bass_groups = _surround_sub_bass(config["bass_management"], destinations)
        elif surround:
            routes, bass_groups = _surround_routes(role, destinations, has_subs=has_subs), []
        else:
            routes, bass_groups = _input_routes(role, topology.sub_mode), []
        outputs.append({
            "role": role, "routes": routes,
            "crossover": filters,
            "bank": _bank_plan(role, config["banks"][role], preset_loader,
                               neutralized=neutralize_banks),
            "gain_db": settings["level_db"] + sum(settings["level_cal_db"].values()),
            "delay_ms": settings["alignment_ms"] + delay_offset,
            "invert": settings["polarity"] == "invert",
        })
        if surround and role in SUB_ROLES:
            if bass_groups:
                outputs[-1]["bass_groups"] = bass_groups
            outputs[-1]["lfe_routes"] = [{"input": 3, "gain": 1.0}]
    indices = {role: index for index, role in enumerate(topology.roles)}
    edges = [{"output": indices[role], "channel": index}
             for index, role in enumerate(assignments[:channels]) if role != "off"]
    plan = {
        "schema": "fxroute.dsp.processing-plan", "version": 1,
        "mode": mode, "device_key": device_key(output_key), "sample_rate_hz": sample_rate_hz,
        "sub_mode": topology.sub_mode, "way_count": topology.way_count,
        "crossover_enabled": xo_enabled,
        "order": ["global", "matrix", "crossover", "area-bank", "output-trim", "guard-mute"],
        "global": global_bank, "outputs": outputs, "physical_routes": edges,
        "av_sync": copy.deepcopy(config['av_sync']),
    }
    if mode == "surround":
        plan["input_positions"] = list(SURROUND_POSITIONS)
    return plan
