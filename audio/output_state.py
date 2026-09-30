# SPDX-License-Identifier: AGPL-3.0-only
"""Versioned per-mode routing, correction banks, and output processing state."""

from __future__ import annotations

import copy
import math
from collections.abc import Callable

from audio.output_routing import device_key
from audio.filter_banks import bank_definitions, resolve_bank, summarize_banks
from audio.output_topology import MAIN_ROLES, MODES, derive_topology, roles_for_mode, validate_assignments
from dsp.banks import BankState

SCHEMA = "fxroute.output-state"
VERSION = 3
FILTER_SLOPES = {
    "linkwitz-riley": tuple(range(12, 73, 12)),
    "butterworth": tuple(range(6, 73, 6)),
    "bessel": tuple(range(6, 73, 6)),
}

# Shared sub crossover defaults: Linkwitz-Riley 24 dB/oct, the classic
# bass-management alignment. A stereo sub pair is coupled until unlinked.
BASS_FILTER_DEFAULT = {"family": "linkwitz-riley", "slope_db_oct": 24}
SUB_SIDES = ("left", "right")
BASS_FIELDS = ("frequency_hz", "main_highpass_enabled", "family", "slope_db_oct",
               "sub_link", "sub_filters")


FILTER_LABELS = {"linkwitz-riley": "LR", "butterworth": "BW", "bessel": "BS"}


def filter_label(family: str, slope_db_oct: object) -> str:
    """Short display label for a filter shape, e.g. LR24, BW12, BS18."""
    return f"{FILTER_LABELS.get(family, str(family))}{slope_db_oct}"


def default_processing() -> dict:
    return {"highpass": None, "lowpass": None, "level_db": 0.0,
            "alignment_ms": 0.0, "polarity": "normal"}


def default_bass_management() -> dict:
    return {"frequency_hz": 80, "main_highpass_enabled": True,
            "family": BASS_FILTER_DEFAULT["family"],
            "slope_db_oct": BASS_FILTER_DEFAULT["slope_db_oct"],
            "sub_link": True, "sub_filters": {}}


def default_output_state() -> dict:
    modes = {}
    for mode in MODES:
        roles = MAIN_ROLES
        modes[mode] = {
            "routing": {}, "crossover_enabled": False, "selected_bank": "global",
            "banks": {role: BankState().to_dict() for role in ("global", *roles)},
            "processing": {role: default_processing() for role in roles},
            "bass_management": default_bass_management(),
            "extras": {},
        }
    return {"schema": SCHEMA, "version": VERSION, "revision": 0,
            "active_mode": "stereo", "modes": modes, "legacy": {}}


def _object_fields(payload: object, fields: set[str], label: str) -> None:
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError(f"{label} requires exactly: {', '.join(sorted(fields))}")


def _finite_range(value: object, minimum: float, maximum: float, label: str) -> None:
    if (type(value) not in (float, int) or not math.isfinite(value)
            or not minimum <= value <= maximum):
        raise ValueError(f"{label} must be finite and between {minimum} and {maximum}")


def _validate_json(value: object) -> None:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("State object keys must be strings")
        for child in value.values():
            _validate_json(child)
    elif isinstance(value, list):
        for child in value:
            _validate_json(child)
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError("State numbers must be finite")
    elif value is not None and type(value) not in (str, bool, int):
        raise ValueError("State values must be JSON-compatible")


def _validate_family_slope(family: object, slope: object, label: str) -> None:
    if not isinstance(family, str) or family not in FILTER_SLOPES:
        raise ValueError(f"Unsupported {label} filter family")
    if type(slope) is not int or slope not in FILTER_SLOPES[family]:
        raise ValueError(f"Unsupported {label} family/slope combination")


def validate_filter(payload: object) -> None:
    if payload is None:
        return
    _object_fields(payload, {"family", "slope_db_oct", "frequency_hz"}, "Crossover filter")
    _validate_family_slope(payload["family"], payload["slope_db_oct"], "crossover")
    _finite_range(payload["frequency_hz"], 20, 20000, "Crossover frequency")


def validate_bass_filter(payload: object) -> None:
    """Sub crossover filter: a way's families/slopes at subwoofer frequencies."""
    _object_fields(payload, {"family", "slope_db_oct", "frequency_hz"}, "Sub crossover filter")
    _validate_family_slope(payload["family"], payload["slope_db_oct"], "sub crossover")
    _finite_range(payload["frequency_hz"], 40, 200, "Sub crossover frequency")


def shared_bass_crossover(bass: dict) -> dict:
    """The mode's shared sub crossover: Mono, Dual-Mono and coupled Stereo."""
    return {"family": bass["family"], "slope_db_oct": bass["slope_db_oct"],
            "frequency_hz": bass["frequency_hz"]}


def bass_crossover_for_side(bass: dict, side: str) -> dict:
    """Effective sub crossover filter for one side of an unlinked stereo pair.

    Only a true Left/Right sub pair resolves per side; dual-mono and mono
    systems always run the shared crossover. A side without a stored
    override falls back to the shared values.
    """
    if side not in SUB_SIDES:
        raise ValueError("Sub crossover side must be left or right")
    if not bass["sub_link"]:
        override = bass["sub_filters"].get(side)
        if override is not None:
            return copy.deepcopy(override)
    return shared_bass_crossover(bass)


def _validate_processing(payload: object) -> None:
    _object_fields(payload, {"highpass", "lowpass", "level_db", "alignment_ms", "polarity"}, "Output processing")
    for key in ("highpass", "lowpass"):
        validate_filter(payload[key])
    if payload["highpass"] and payload["lowpass"]:
        if payload["highpass"]["frequency_hz"] >= payload["lowpass"]["frequency_hz"]:
            raise ValueError("Output high-pass frequency must be below low-pass frequency")
    _finite_range(payload["level_db"], -80, 24, "Output level")
    _finite_range(payload["alignment_ms"], -40, 40, "Output alignment")
    if payload["polarity"] not in ("normal", "invert"):
        raise ValueError("Output polarity must be normal or invert")


def _device_identity(key: object) -> str:
    if not isinstance(key, str) or not key.strip() or key != key.strip():
        raise ValueError("Output device key must be a non-empty string")
    return device_key(key)


def validate_output_state(payload: object) -> dict:
    """Validate inactive modes too; never discard unknown or invalid settings."""
    _validate_json(payload)
    _object_fields(payload, {"schema", "version", "revision", "active_mode", "modes", "legacy"}, "Output state")
    if payload["schema"] != SCHEMA or type(payload["version"]) is not int or payload["version"] != VERSION:
        raise ValueError("Unsupported output state schema or version")
    if type(payload["revision"]) is not int or payload["revision"] < 0:
        raise ValueError("State revision must be a non-negative integer")
    if payload["active_mode"] not in MODES:
        raise ValueError("Unsupported active output mode")
    _object_fields(payload["modes"], set(MODES), "Output modes")
    if not isinstance(payload["legacy"], dict):
        raise ValueError("Legacy snapshot must be an object")
    for mode, config in payload["modes"].items():
        _object_fields(config, {"routing", "crossover_enabled", "selected_bank", "banks", "processing", "bass_management", "extras"}, "Mode state")
        enabled = config["crossover_enabled"]
        roles_for_mode(mode, crossover_enabled=enabled)
        # Dormant banks retain their filters when crossover is toggled.
        allowed = (*roles_for_mode(mode), *roles_for_mode(mode, crossover_enabled=True))
        banks, processing, routing = config["banks"], config["processing"], config["routing"]
        if not isinstance(banks, dict) or "global" not in banks:
            raise ValueError("Every mode requires a Global bank")
        if any(role not in ("global", *allowed) for role in banks):
            raise ValueError("Bank role is not valid for its output mode")
        for bank in banks.values():
            BankState.from_dict(bank)
        selected = config["selected_bank"]
        if not isinstance(selected, str) or selected not in {"all", *banks, *bank_definitions(banks)}:
            raise ValueError("Selected bank is not stored in its output mode")
        if not isinstance(processing, dict) or set(processing) != set(banks) - {"global"}:
            raise ValueError("Every area bank requires corresponding output processing")
        for settings in processing.values():
            _validate_processing(settings)
        if not isinstance(routing, dict):
            raise ValueError("Mode routing must be an object keyed by device")
        for key, assignments in routing.items():
            if _device_identity(key) != key:
                raise ValueError("Stored routing requires normalized device keys")
            roles = validate_assignments(mode, assignments, crossover_enabled=enabled)
            if any(role != "off" and role not in banks for role in roles):
                raise ValueError("Every assigned role requires an area bank")
        bass = config["bass_management"]
        _object_fields(bass, set(BASS_FIELDS), "Bass management")
        _finite_range(bass["frequency_hz"], 40, 200, "Sub crossover frequency")
        if type(bass["main_highpass_enabled"]) is not bool:
            raise ValueError("Main high-pass enabled must be a boolean")
        _validate_family_slope(bass["family"], bass["slope_db_oct"], "sub crossover")
        if type(bass["sub_link"]) is not bool:
            raise ValueError("Sub L/R link must be a boolean")
        overrides = bass["sub_filters"]
        if not isinstance(overrides, dict) or any(side not in SUB_SIDES for side in overrides):
            raise ValueError("Sub crossover overrides must be keyed by left and right")
        for side, definition in overrides.items():
            try:
                validate_bass_filter(definition)
            except ValueError as exc:
                raise ValueError(f"Sub crossover override {side}: {exc}") from exc
        if not isinstance(config["extras"], dict):
            raise ValueError("Global helpers must be an object")
    return copy.deepcopy(payload)


def routing_for_device(state: dict, mode: str, output_key: str) -> list[str]:
    roles_for_mode(mode)
    fallback = [] if state["modes"][mode]["crossover_enabled"] else list(MAIN_ROLES)
    return list(state["modes"][mode]["routing"].get(_device_identity(output_key), fallback))


def set_mode_routing(state: dict, mode: str, output_key: str, assignments: object) -> dict:
    """Update visible ports while retaining assignments in a higher hardware tier."""
    result = validate_output_state(state)
    config = result["modes"][mode]
    values = list(validate_assignments(mode, assignments, crossover_enabled=config["crossover_enabled"]))
    count = len(values)
    key = _device_identity(output_key)
    config = result["modes"][mode]
    values.extend(config["routing"].get(key, [])[count:])
    config["routing"][key] = values
    for role in values:
        if role != "off":
            config["banks"].setdefault(role, BankState().to_dict())
            config["processing"].setdefault(role, default_processing())
    topology = derive_topology(mode, values, channels=count, crossover_enabled=config["crossover_enabled"])
    active = topology.bank_ids
    try:
        selected = resolve_bank(config, config["selected_bank"], topology.roles)["id"]
    except ValueError:
        selected = "global"
    if selected not in (*active, "all"):
        selected = "global"
    if set(topology.roles) == {"main_l", "main_r"}:
        selected = "global"
    config["selected_bank"] = selected
    return validate_output_state(result)


def switch_mode(state: dict, mode: str) -> dict:
    roles_for_mode(mode)
    result = validate_output_state(state)
    result["active_mode"] = mode
    return result


def set_crossover(state: dict, mode: str, enabled: bool) -> dict:
    """Change the role domain in the same routing, retaining dormant DSP banks.

    Main ports become Low ports on enable. On disable only Low ports become
    Main; higher ways turn Off. No second crossover routing is stored.
    """
    roles_for_mode(mode, crossover_enabled=enabled)
    result = validate_output_state(state)
    config = result["modes"][mode]
    if config["crossover_enabled"] == enabled:
        return result
    mapping = {"main_l": "left_low", "main_r": "right_low"} if enabled else {
        "left_low": "main_l", "right_low": "main_r"}
    allowed = (*roles_for_mode(mode, crossover_enabled=enabled), "off")
    config["crossover_enabled"] = enabled
    for key, assignments in config["routing"].items():
        config["routing"][key] = [mapping.get(role, role if role in allowed else "off") for role in assignments]
        for role in config["routing"][key]:
            if role != "off":
                config["banks"].setdefault(role, BankState().to_dict())
                config["processing"].setdefault(role, default_processing())
    if config["selected_bank"] not in ("global", "all", *allowed, *bank_definitions(allowed)):
        config["selected_bank"] = "global"
    return validate_output_state(result)


_UNCHANGED: object = object()


def set_bank_preset(state: dict, mode: str, bank_id: str, *, preset: str | None = None,
                    preset_a: str | None = None, preset_b: object = _UNCHANGED,
                    active_side: str | None = None, roles=None) -> dict:
    """Assign bank presets without touching routing or other banks.

    ``preset`` writes the currently listened slot (slot A when the bank
    listens to neither slot yet) and listens to it. ``active_side`` alone
    switches listening; an unassigned B side is rejected. Slot arguments
    set compare slots directly; an explicit ``preset_b=None`` clears B.
    """
    result = validate_output_state(state)
    roles_for_mode(mode)
    banks = result["modes"][mode]["banks"]
    members = resolve_bank(result["modes"][mode], bank_id, roles)["roles"]
    if active_side is not None and active_side not in {"A", "B"}:
        raise ValueError("Compare side must be A or B")
    slot = active_side or summarize_banks([banks[role] for role in members])["active_side"] or "A"
    for role in members:
        current = dict(banks[role])
        if preset_a is not None:
            current["preset_a"] = preset_a
        if preset_b is not _UNCHANGED:
            current["preset_b"] = preset_b
        if preset is not None:
            current[f"preset_{slot.lower()}"] = preset
            current["preset"] = preset
        elif active_side is not None:
            target = current["preset_b"] if active_side == "B" else current["preset_a"]
            if target is None:
                raise ValueError("Compare side B has no assigned preset")
            current["preset"] = target
        banks[role] = BankState.from_dict(current).to_dict()
    return validate_output_state(result)


def switch_all_banks(state: dict, mode: str, output_key: str, channels: int, active_side: str) -> dict:
    """Switch configured area bindings atomically, excluding Global and dormant roles.

    B needs a B slot in at least one bank; banks without one stay on A.
    """
    result = validate_output_state(state)
    config = result["modes"][mode]
    topology = derive_topology(mode, routing_for_device(result, mode, output_key), channels=channels,
                               crossover_enabled=config["crossover_enabled"])
    if not topology.roles:
        raise ValueError("No configured area banks")
    if active_side not in {"A", "B"}:
        raise ValueError("Compare side must be A or B")
    comparable = {role for role in topology.roles if config["banks"][role]["preset_b"] is not None}
    if active_side == "B" and not comparable:
        raise ValueError("Compare side B has no assigned preset in any configured bank")
    for role in topology.roles:
        side = active_side if role in comparable else "A"
        config["banks"][role] = BankState.from_dict(config["banks"][role]).select(side).to_dict()
    return validate_output_state(result)


def set_output_processing(state: dict, mode: str, role: str, *, highpass: object = _UNCHANGED,
                          lowpass: object = _UNCHANGED, level_db: float | None = None,
                          alignment_ms: float | None = None,
                          polarity: str | None = None) -> dict:
    """Edit one area's output processing; filters accept None to clear."""
    result = validate_output_state(state)
    roles_for_mode(mode)
    processing = result["modes"][mode]["processing"]
    if role not in processing:
        raise ValueError(f"Role {role} has no output processing in output mode {mode}")
    settings = processing[role]
    if highpass is not _UNCHANGED:
        validate_filter(highpass)
        settings["highpass"] = highpass
    if lowpass is not _UNCHANGED:
        validate_filter(lowpass)
        settings["lowpass"] = lowpass
    if level_db is not None:
        settings["level_db"] = level_db
    if alignment_ms is not None:
        settings["alignment_ms"] = alignment_ms
    if polarity is not None:
        settings["polarity"] = polarity
    return validate_output_state(result)


def set_bass_management(state: dict, mode: str, *, frequency_hz: float | None = None,
                        main_highpass_enabled: bool | None = None,
                        family: str | None = None, slope_db_oct: int | None = None,
                        sub_link: bool | None = None,
                        sub_filters: dict | None = None) -> dict:
    """Edit the mode's sub crossover; a per-side override accepts None to clear."""
    result = validate_output_state(state)
    roles_for_mode(mode)
    bass = result["modes"][mode]["bass_management"]
    if frequency_hz is not None:
        bass["frequency_hz"] = frequency_hz
    if main_highpass_enabled is not None:
        bass["main_highpass_enabled"] = main_highpass_enabled
    if family is not None or slope_db_oct is not None:
        if family is None or slope_db_oct is None:
            raise ValueError("Sub crossover type and slope must be set together")
        bass["family"] = family
        bass["slope_db_oct"] = slope_db_oct
    if sub_link is not None:
        bass["sub_link"] = sub_link
    if sub_filters is not None:
        if not isinstance(sub_filters, dict):
            raise ValueError("Sub crossover overrides must be an object keyed by side")
        for side, definition in sub_filters.items():
            if side not in SUB_SIDES:
                raise ValueError("Sub crossover overrides must be keyed by left and right")
            if definition is None:
                bass["sub_filters"].pop(side, None)
            else:
                bass["sub_filters"][side] = definition
    return validate_output_state(result)


def set_mode_extras(state: dict, mode: str, extras: dict) -> dict:
    if not isinstance(extras, dict):
        raise ValueError("Global helpers must be an object")
    result = validate_output_state(state)
    roles_for_mode(mode)
    result["modes"][mode]["extras"] = extras
    return validate_output_state(result)


def select_bank(state: dict, mode: str, output_key: str, channels: int, bank_id: str) -> dict:
    result = validate_output_state(state)
    active = derive_topology(mode, routing_for_device(result, mode, output_key), channels=channels,
                             crossover_enabled=result["modes"][mode]["crossover_enabled"])
    if set(active.roles) == {"main_l", "main_r"} and bank_id != "global":
        raise ValueError("Pure stereo uses the Global bank; no bank selection is shown")
    if bank_id == "all":
        if not active.roles:
            raise ValueError("No configured area banks")
        result["modes"][mode]["selected_bank"] = "all"
        return result
    bank_id = resolve_bank(result["modes"][mode], bank_id, active.roles)["id"]
    if bank_id not in active.bank_ids:
        raise ValueError("Bank is not configured on the available hardware outputs")
    result["modes"][mode]["selected_bank"] = bank_id
    return validate_output_state(result)


def release_bank_presets(state: dict, drop: Callable[[str, str], bool]) -> tuple[dict, list[str]]:
    """Clear every bank slot whose preset ``drop(role, name)`` selects.

    Global's compare rules for a preset that goes away: a released A falls
    back to Neutral, a released B is cleared, and a role listening to a
    released slot falls back to A. Returns the new state and one line per
    touched role.
    """
    result = validate_output_state(state)
    changes = []
    for mode, config in result["modes"].items():
        for role, binding in config["banks"].items():
            def released(name):
                return name is not None and drop(role, name)

            names = [name for name in dict.fromkeys(binding.values()) if released(name)]
            if not names:
                continue
            side = BankState.from_dict(binding).active_side
            preset_a = "Neutral" if released(binding["preset_a"]) else binding["preset_a"]
            preset_b = binding["preset_b"]
            if released(preset_b) or preset_b == preset_a:
                preset_b = None
            if side == "B" and preset_b is not None:
                preset = preset_b
            elif side is None and not released(binding["preset"]):
                preset = binding["preset"]
            else:
                preset = preset_a
            config["banks"][role] = BankState(preset, preset_a, preset_b).to_dict()
            changes.append(f"{mode}/{role}: released {', '.join(map(repr, names))}; "
                           f"A={preset_a!r} B={preset_b!r} listening={preset!r}")
    return validate_output_state(result), changes


def referenced_presets(state: dict) -> set[str]:
    validated = validate_output_state(state)
    return {name for mode in validated["modes"].values() for bank in mode["banks"].values()
            for name in bank.values() if name is not None}
