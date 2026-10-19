# SPDX-License-Identifier: AGPL-3.0-only
"""Output-state HTTP validation and read models, independent of live graph writes."""

import cmath
import math

from fastapi import HTTPException

from audio import hdmi
from audio.filter_banks import bank_catalog, resolve_bank, selected_bank, summarize_all_banks
from audio.output_context import output_state_device, output_state_topology
from audio.output_state import (
    FILTER_SLOPES, bass_crossover_for_group, bass_crossover_for_side, roles_for_mode,
    routing_for_device, select_bank, set_av_sync, set_bank_preset, set_bass_management,
    set_crossover, set_mode_extras, set_mode_routing, set_output_processing,
    shared_bass_crossover, switch_all_banks, switch_mode,
)
from audio.output_topology import CENTER_ROLES, MODES, SPEAKER_ROLES, side_for_role, surround_group
from dsp.crossover import design_crossover
from dsp.manager import DSPManager


def require_state_mode(value) -> str:
    mode = str(value or "").strip()
    if mode not in MODES:
        raise HTTPException(status_code=400, detail=f"Unknown output mode: {value}")
    return mode


def _require_bank_id(value) -> str:
    bank_id = str(value or "").strip()
    if not bank_id:
        raise HTTPException(status_code=400, detail="bank_id is required")
    return bank_id


def _require_role(value) -> str:
    role = str(value or "").strip()
    if not role:
        raise HTTPException(status_code=400, detail="role is required")
    return role


def build_output_state_mutation(mutation: dict, *, output_key: str, channels: int | None,
                                get_service, validate_level_result):
    if not isinstance(mutation, dict):
        raise HTTPException(status_code=400, detail="mutation must be an object")
    kind = mutation.get("kind")
    fields = {key: value for key, value in mutation.items() if key != "kind"}

    def strict(allowed: set[str]) -> dict:
        unknown = set(fields) - allowed
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown mutation fields: {sorted(unknown)}")
        return fields

    if kind == "set_routing":
        args = strict({"mode", "assignments"})
        mode = require_state_mode(args.get("mode"))
        assignments = args.get("assignments")
        if channels is not None and int(channels) == 2:
            raise HTTPException(status_code=400,
                                detail="Output routing is fixed to Out 1 Main L / Out 2 Main R on two-output devices")
        return lambda state: set_mode_routing(state, mode, output_key, assignments)
    if kind == "switch_mode":
        args = strict({"mode"})
        mode = require_state_mode(args.get("mode"))
        return lambda state: switch_mode(state, mode)
    if kind == "set_crossover":
        args = strict({"mode", "enabled"})
        mode = require_state_mode(args.get("mode"))
        return lambda state: set_crossover(state, mode, args.get("enabled"))
    if kind == "set_subwoofers":
        args = strict({"mode", "frequency_hz", "main_highpass_enabled", "processing",
                       "family", "slope_db_oct", "sub_link", "sub_filters"})
        mode = require_state_mode(args.get("mode"))

        def update_subwoofers(state):
            topology = output_state_topology(state, mode, output_key, channels)
            processing = args.get("processing")
            if not isinstance(processing, dict) or set(processing) != set(topology["sub_roles"]):
                raise ValueError("Sub settings must describe exactly the routed sub roles")
            result = set_bass_management(
                state, mode, frequency_hz=args.get("frequency_hz"),
                main_highpass_enabled=args.get("main_highpass_enabled"),
                family=args.get("family"), slope_db_oct=args.get("slope_db_oct"),
                sub_link=args.get("sub_link"), sub_filters=args.get("sub_filters"))
            for role, settings in processing.items():
                if not isinstance(settings, dict) or set(settings) != {"level_db", "alignment_ms", "polarity"}:
                    raise ValueError("Sub settings require level, alignment and polarity")
                result = set_output_processing(result, mode, role, **settings)
            return result
        return update_subwoofers
    if kind == "select_bank":
        args = strict({"mode", "bank_id"})
        if channels is None:
            raise HTTPException(status_code=400, detail="Output channel capacity is unknown")
        mode = require_state_mode(args.get("mode"))
        bank_id = _require_bank_id(args.get("bank_id"))
        return lambda state: select_bank(state, mode, output_key, channels, bank_id)
    if kind == "set_bank_preset":
        # Surround Global names the speaker group a preset is selected for.
        args = strict({"mode", "bank_id", "preset", "preset_a", "preset_b", "active_side", "group"})
        mode = require_state_mode(args.get("mode"))
        bank_id = _require_bank_id(args.get("bank_id"))
        options = {name: args[name] for name in ("preset", "preset_a", "preset_b", "active_side", "group")
                   if name in args}

        def update_bank(state):
            topology = output_state_topology(state, mode, output_key, channels)
            definition = resolve_bank(state["modes"][mode], bank_id, topology["roles"])
            for name in ("preset", "preset_a", "preset_b"):
                if options.get(name) is not None:
                    try:
                        get_service().validate_bank_preset(state, mode, definition["id"], options[name],
                                                           roles=topology["roles"], group=options.get("group"))
                    except FileNotFoundError as exc:
                        raise ValueError(f"Unknown preset {options[name]!r}") from exc
            return set_bank_preset(state, mode, definition["id"], roles=topology["roles"], **options)
        return update_bank
    if kind == "switch_all_banks":
        args = strict({"mode", "active_side"})
        mode = require_state_mode(args.get("mode"))
        if channels is None:
            raise HTTPException(status_code=400, detail="Output channel capacity is unknown")
        return lambda state: switch_all_banks(state, mode, output_key, channels, args.get("active_side"))
    if kind == "set_processing":
        args = strict({"mode", "role", "highpass", "lowpass", "level_db",
                       "alignment_ms", "polarity", "level_cal_db"})
        mode = require_state_mode(args.get("mode"))
        role = _require_role(args.get("role"))
        options = {name: args[name] for name in ("highpass", "lowpass") if name in args}
        options.update({name: args[name] for name in ("level_db", "alignment_ms", "polarity", "level_cal_db")
                        if args.get(name) is not None})
        return lambda state: set_output_processing(state, mode, role, **options)
    if kind == "set_level_calibration":
        args = strict({"mode", "values", "state_revision", "measurement_job_id"})
        mode = require_state_mode(args.get("mode"))
        values = args.get("values")
        if not isinstance(values, dict) or not values:
            raise HTTPException(status_code=400, detail="values must map roles to level calibration slots")

        def update_level_calibration(state):
            validate_level_result(args, state)
            result = state
            for role, slots in values.items():
                result = set_output_processing(result, mode, _require_role(role), level_cal_db=slots)
            return result
        return update_level_calibration
    if kind == "set_extras":
        args = strict({"mode", "extras"})
        mode = require_state_mode(args.get("mode"))
        if not isinstance(args.get("extras"), dict):
            raise HTTPException(status_code=400, detail="extras must be an object")
        return lambda state: set_mode_extras(state, mode, args["extras"])
    if kind == 'set_av_sync':
        args = strict({'mode', 'target_ms'})
        mode = require_state_mode(args.get('mode'))
        return lambda state: set_av_sync(state, mode, target_ms=args.get('target_ms'))
    raise HTTPException(status_code=400, detail=f"Unknown mutation kind: {kind}")


def output_channel_labels(overview: dict, output_key: str) -> list:
    """Receiver channel per hardware output of an HDMI device (else empty).

    Labels follow the sink's resolved PipeWire ports, the same list the DSP
    links Out 1 … Out N to.
    """
    if not hdmi.is_hdmi_key(output_key):
        return []
    mode = overview.get("output_mode") or {}
    ports = mode.get("hardware_playback_ports") or mode.get("hardware_playback_ports_from_channel_map") or []
    return hdmi.channel_labels(ports)


def output_state_response(state: dict, overview: dict) -> dict:
    output_key, channels = output_state_device(overview)
    modes = {}
    for mode, config in state["modes"].items():
        topology = output_state_topology(state, mode, output_key, channels)
        banks = bank_catalog(config, topology["roles"])
        if mode == "surround" and "main" in banks:
            banks["main"]["label"] = "Front L/R"
        modes[mode] = {
            "crossover_enabled": config["crossover_enabled"],
            "selected_bank": selected_bank(config, topology["roles"], mode),
            "banks": banks,
            "all_banks": summarize_all_banks([config["banks"][role] for role in topology["roles"]]),
            "processing": config["processing"],
            "bass_management": config["bass_management"],
            "extras": config["extras"],
            "av_sync": config['av_sync'],
            "topology": topology,
        }
    return {
        "status": "ok", "revision": state["revision"], "active_mode": state["active_mode"],
        "device": {"key": output_key, "channels": channels, "hdmi": hdmi.is_hdmi_key(output_key),
                   "channel_labels": output_channel_labels(overview, output_key),
                   "routing": {mode: routing_for_device(state, mode, output_key, channels) for mode in MODES}},
        "modes": modes,
        "capabilities": {
            "modes": list(MODES),
            "roles": {mode: list(roles_for_mode(mode, crossover_enabled=state["modes"][mode]["crossover_enabled"]))
                      for mode in MODES},
            "filter_families": {family: list(slopes) for family, slopes in FILTER_SLOPES.items()},
            "max_slope_db_oct": 72, "max_biquads_per_output": DSPManager.OUTPUT_FILTER_MAX_BIQUADS,
        },
    }


def _crossover_bass_highpass(mode_config: dict, topology: dict, role: str) -> dict | None:
    """Include the DSP's derived bass high-pass in the displayed way response."""
    bass = (mode_config or {}).get("bass_management") or {}
    sub_roles = (topology or {}).get("sub_roles") or []
    if not sub_roles:
        return None
    try:
        if topology.get("mode") == "surround":
            definition, enabled = bass_crossover_for_group(bass, surround_group(role))
            if not enabled:
                return None
        else:
            if bass.get("main_highpass_enabled") is not True:
                return None
            definition = (bass_crossover_for_side(bass, side_for_role(role))
                          if (topology or {}).get("sub_mode") == "stereo"
                          else shared_bass_crossover(bass))
        frequency = float(definition["frequency_hz"])
    except (TypeError, ValueError, KeyError):
        return None
    if not 40 <= frequency <= 200:
        return None
    return {"family": definition["family"], "slope_db_oct": definition["slope_db_oct"],
            "frequency_hz": int(round(frequency))}


def _crossover_way_required(role: str) -> tuple[str, ...]:
    if role.endswith("low"):
        return ("lowpass",)
    if role.endswith("high"):
        return ("highpass",)
    return ("highpass", "lowpass")


def _crossover_way_points(role: str, role_settings: dict, sample_rate_hz: int,
                          point_count: int = 180, extra_highpass: dict | None = None) -> list | None:
    """Evaluate stored way filters and the optional derived bass high-pass."""
    try:
        sections = []
        for kind in ("highpass", "lowpass"):
            definition = role_settings.get(kind)
            if definition is None:
                continue
            sections.extend(design_crossover(
                {"kind": kind, "family": definition["family"],
                 "slope_db_oct": definition["slope_db_oct"],
                 "frequency_hz": definition["frequency_hz"]}, sample_rate_hz))
        if extra_highpass is not None:
            sections.extend(design_crossover(
                {"kind": "highpass", "family": extra_highpass["family"],
                 "slope_db_oct": extra_highpass["slope_db_oct"],
                 "frequency_hz": extra_highpass["frequency_hz"]}, sample_rate_hz))
    except (ValueError, KeyError, TypeError):
        return None
    points = []
    for index in range(point_count):
        frequency = 20.0 * (20000.0 / 20.0) ** (index / (point_count - 1))
        total = 1.0 + 0.0j
        for section in sections:
            b0, b1, b2, _, a1, a2 = section
            z = cmath.exp(2j * math.pi * frequency / sample_rate_hz)
            total *= (b0 + b1 / z + b2 / z / z) / (1.0 + a1 / z + a2 / z / z)
        points.append([round(frequency, 3), round(20.0 * math.log10(abs(total)), 3)])
    return points


def crossover_response(state: dict, overview: dict, status: dict) -> dict:
    rate = status.get("active_rate")
    if not isinstance(rate, int) or rate <= 0:
        rate = status.get("force_rate")
    if not isinstance(rate, int) or rate <= 0:
        rate = 48000
    mode = state["active_mode"]
    ways = {}
    output_key, channels = output_state_device(overview)
    topology = output_state_topology(state, mode, output_key, channels)
    mode_config = state["modes"][mode]
    for role, settings in mode_config["processing"].items():
        if not topology["crossover_enabled"] or role not in topology["roles"]:
            continue
        if role not in (*SPEAKER_ROLES, *CENTER_ROLES):
            continue
        bass_highpass = _crossover_bass_highpass(mode_config, topology, role)
        ways[role] = {
            "filters": {"highpass": settings["highpass"], "lowpass": settings["lowpass"]},
            "derived_highpass": dict(bass_highpass) if bass_highpass else None,
            "complete": all(settings.get(kind) is not None for kind in _crossover_way_required(role)),
            "points": _crossover_way_points(role, settings, rate, extra_highpass=bass_highpass),
        }
    return {"status": "ok", "revision": state["revision"], "mode": mode,
            "crossover_enabled": topology["crossover_enabled"],
            "bass_management": dict(mode_config["bass_management"]),
            "sub_roles": list(topology["sub_roles"]), "sample_rate_hz": rate, "ways": ways}
