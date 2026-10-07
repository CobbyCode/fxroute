# SPDX-License-Identifier: AGPL-3.0-only
"""Versioned per-mode routing, correction banks, and output processing state."""

from __future__ import annotations

import copy
import math
from collections.abc import Callable

from audio.output_routing import device_key
from audio.filter_banks import bank_definitions, resolve_bank, summarize_banks
from audio.output_topology import (MAIN_ROLES, MODES, SURROUND_GROUPS, derive_topology, dormant_roles,
                                   roles_for_mode, validate_assignments)
from dsp.banks import BankState

SCHEMA = "fxroute.output-state"
VERSION = 5
# Level Calibration slots per role: "global" holds the whole-speaker match
# (Global measurement; Surround Front), "bank" the match of the role's own bank.
LEVEL_CAL_SLOTS = ("global", "bank")
LEVEL_CAL_MIN_DB = -80
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


def default_level_cal() -> dict:
    return {slot: 0.0 for slot in LEVEL_CAL_SLOTS}


def default_processing() -> dict:
    return {"highpass": None, "lowpass": None, "level_db": 0.0,
            "alignment_ms": 0.0, "polarity": "normal",
            "level_cal_db": default_level_cal()}


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
            "active_mode": "stereo", "modes": modes, "legacy": {}, "input_modes": {}}


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


def validate_group_bass_filter(payload: object) -> None:
    """Surround group crossover: the sub crossover shape plus its own high-pass switch."""
    _object_fields(payload, {"family", "slope_db_oct", "frequency_hz", "highpass_enabled"},
                   "Speaker group crossover")
    validate_bass_filter({key: payload[key] for key in ("family", "slope_db_oct", "frequency_hz")})
    if type(payload["highpass_enabled"]) is not bool:
        raise ValueError("Speaker group high-pass enabled must be a boolean")


def bass_override_keys(mode: str) -> tuple[str, ...]:
    """Unlinked crossover keys: L/R sub sides, or Surround speaker groups."""
    return SURROUND_GROUPS if mode == "surround" else SUB_SIDES


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


def bass_crossover_for_group(bass: dict, group: str) -> tuple[dict, bool]:
    """Effective Surround crossover and high-pass switch of one speaker group.

    Linked groups share the mode crossover and Main high-pass switch; an
    unlinked group without a stored override falls back to them.
    """
    if group not in SURROUND_GROUPS:
        raise ValueError("Unknown Surround speaker group")
    override = None if bass["sub_link"] else bass["sub_filters"].get(group)
    if override is None:
        return shared_bass_crossover(bass), bool(bass["main_highpass_enabled"])
    return ({key: override[key] for key in ("family", "slope_db_oct", "frequency_hz")},
            bool(override["highpass_enabled"]))


def validate_level_cal(payload: object) -> None:
    """Level Calibration is cut-only: the per-output gain sits behind the protection limiter."""
    _object_fields(payload, set(LEVEL_CAL_SLOTS), "Level calibration")
    for slot in LEVEL_CAL_SLOTS:
        _finite_range(payload[slot], LEVEL_CAL_MIN_DB, 0, f"Level calibration {slot}")


def _validate_processing(payload: object) -> None:
    _object_fields(payload, {"highpass", "lowpass", "level_db", "alignment_ms", "polarity",
                             "level_cal_db"}, "Output processing")
    for key in ("highpass", "lowpass"):
        validate_filter(payload[key])
    if payload["highpass"] and payload["lowpass"]:
        if payload["highpass"]["frequency_hz"] >= payload["lowpass"]["frequency_hz"]:
            raise ValueError("Output high-pass frequency must be below low-pass frequency")
    _finite_range(payload["level_db"], -80, 24, "Output level")
    _finite_range(payload["alignment_ms"], -40, 40, "Output alignment")
    if payload["polarity"] not in ("normal", "invert"):
        raise ValueError("Output polarity must be normal or invert")
    validate_level_cal(payload["level_cal_db"])


def _device_identity(key: object) -> str:
    if not isinstance(key, str) or not key.strip() or key != key.strip():
        raise ValueError("Output device key must be a non-empty string")
    return device_key(key)


def _validate_bass_overrides(mode: str, overrides: object) -> None:
    keys = bass_override_keys(mode)
    if not isinstance(overrides, dict) or any(key not in keys for key in overrides):
        raise ValueError(f"Sub crossover overrides must be keyed by {', '.join(keys)}")
    validate = validate_group_bass_filter if mode == "surround" else validate_bass_filter
    for key, definition in overrides.items():
        try:
            validate(definition)
        except ValueError as exc:
            raise ValueError(f"Sub crossover override {key}: {exc}") from exc


def validate_output_state(payload: object) -> dict:
    """Validate inactive modes too; never discard unknown or invalid settings."""
    _validate_json(payload)
    _object_fields(payload, {"schema", "version", "revision", "active_mode", "modes", "legacy", "input_modes"}, "Output state")
    if payload["schema"] != SCHEMA or type(payload["version"]) is not int or payload["version"] != VERSION:
        raise ValueError("Unsupported output state schema or version")
    if type(payload["revision"]) is not int or payload["revision"] < 0:
        raise ValueError("State revision must be a non-negative integer")
    if payload["active_mode"] not in MODES:
        raise ValueError("Unsupported active output mode")
    if (not isinstance(payload["input_modes"], dict)
            or any(not isinstance(key, str) or not key.strip() or mode not in MODES
                   for key, mode in payload["input_modes"].items())):
        raise ValueError("Input output modes require source identities and supported modes")
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
            # Dormant crossover-path assignments stay stored while the
            # crossover is off (see routing_for_device).
            roles = validate_assignments(mode, assignments, crossover_enabled=enabled, allow_dormant=True)
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
        _validate_bass_overrides(mode, bass["sub_filters"])
        if not isinstance(config["extras"], dict):
            raise ValueError("Global helpers must be an object")
    return copy.deepcopy(payload)


def routing_for_device(state: dict, mode: str, output_key: str, channels: int | None = None) -> list[str]:
    """Active routing, or the fixed stereo mapping on two-output devices.

    A device with exactly two hardware outputs always plays Out 1 = Main L
    and Out 2 = Main R; custom routing is only meaningful with more outputs.
    Passing the effective channel count returns that fixed mapping so every
    caller renders the same topology instead of slicing a stale stored list.

    Crossover-path assignments outside the mode's current domain are dormant
    and read as Off here, so with the crossover off the stored ways never
    reach the plan, the meters or the routing grid.
    """
    roles_for_mode(mode)
    if channels is not None:
        try:
            count = int(channels)
        except (TypeError, ValueError):
            count = None
        if count == 2:
            return list(MAIN_ROLES)
    config = state["modes"][mode]
    fallback = [] if config["crossover_enabled"] else list(MAIN_ROLES)
    stored = list(config["routing"].get(_device_identity(output_key), fallback))
    dormant = set(dormant_roles(mode, crossover_enabled=config["crossover_enabled"]))
    return ["off" if role in dormant else role for role in stored]


def effective_crossover_enabled(state: dict, mode: str, channels: int | None = None) -> bool:
    """Stored crossover flag, forced off on two-output devices.

    Two outputs can only carry Main L/R, so the effective system is Stereo
    without crossover even when the stored mode still enables it (e.g. after
    switching back from a multichannel interface).
    """
    if channels is not None:
        try:
            count = int(channels)
        except (TypeError, ValueError):
            count = None
        if count == 2:
            return False
    return bool(state["modes"][mode]["crossover_enabled"])


def fallback_bank(mode: str, roles) -> str:
    """Selection when the stored bank is gone: Surround falls back to All Banks."""
    return "all" if mode == "surround" and tuple(roles) else "global"


def pure_stereo_roles(mode: str, roles) -> bool:
    """Plain Main L/R outside Surround keeps the single Global A/B surface."""
    return mode != "surround" and set(roles) == set(MAIN_ROLES)


def set_mode_routing(state: dict, mode: str, output_key: str, assignments: object) -> dict:
    """Update visible ports while retaining assignments in a higher hardware tier.

    A dormant crossover way keeps its port until another role claims it: the
    plain topology cannot switch the stored ways off, only replace them, so
    the configured multi-way system survives a crossover excursion.
    """
    result = validate_output_state(state)
    config = result["modes"][mode]
    values = list(validate_assignments(mode, assignments, crossover_enabled=config["crossover_enabled"]))
    count = len(values)
    key = _device_identity(output_key)
    config = result["modes"][mode]
    stored = config["routing"].get(key, [])
    dormant = set(dormant_roles(mode, crossover_enabled=config["crossover_enabled"]))
    values = [stored[index] if (value == "off" and index < len(stored) and stored[index] in dormant)
              else value for index, value in enumerate(values)]
    values.extend(stored[count:])
    config["routing"][key] = values
    for role in values:
        if role != "off":
            config["banks"].setdefault(role, BankState().to_dict())
            config["processing"].setdefault(role, default_processing())
    masked = ["off" if value in dormant else value for value in values]
    topology = derive_topology(mode, masked, channels=count, crossover_enabled=config["crossover_enabled"])
    active = topology.bank_ids
    fallback = fallback_bank(mode, topology.roles)
    try:
        selected = resolve_bank(config, config["selected_bank"], topology.roles)["id"]
    except ValueError:
        selected = fallback
    if selected not in (*active, "all"):
        selected = fallback
    if pure_stereo_roles(mode, topology.roles):
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
    Main; the higher ways stay stored as dormant crossover-path assignments,
    so switching the crossover back on restores the same ways on the same
    ports. No second crossover routing is stored.
    """
    roles_for_mode(mode, crossover_enabled=enabled)
    result = validate_output_state(state)
    config = result["modes"][mode]
    if config["crossover_enabled"] == enabled:
        return result
    mapping = {"main_l": "left_low", "main_r": "right_low"} if enabled else {
        "left_low": "main_l", "right_low": "main_r"}
    allowed = (*roles_for_mode(mode, crossover_enabled=enabled), "off")
    domain = (*roles_for_mode(mode), *roles_for_mode(mode, crossover_enabled=True), "off")
    config["crossover_enabled"] = enabled
    for key, assignments in config["routing"].items():
        config["routing"][key] = [mapping.get(role, role if role in domain else "off") for role in assignments]
        for role in config["routing"][key]:
            if role != "off":
                config["banks"].setdefault(role, BankState().to_dict())
                config["processing"].setdefault(role, default_processing())
    if config["selected_bank"] not in ("global", "all", *allowed, *bank_definitions(allowed)):
        config["selected_bank"] = "all" if mode == "surround" else "global"
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
    topology = derive_topology(mode, routing_for_device(result, mode, output_key, channels), channels=channels,
                               crossover_enabled=effective_crossover_enabled(result, mode, channels))
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
                          polarity: str | None = None,
                          level_cal_db: dict | None = None) -> dict:
    """Edit one area's output processing; filters accept None to clear.

    ``level_cal_db`` merges the given slots and leaves the others untouched.
    """
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
    if level_cal_db is not None:
        if not isinstance(level_cal_db, dict) or not set(level_cal_db) <= set(LEVEL_CAL_SLOTS):
            raise ValueError(f"Level calibration accepts only the slots: {', '.join(LEVEL_CAL_SLOTS)}")
        settings["level_cal_db"] = {**settings["level_cal_db"], **level_cal_db}
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
            raise ValueError("Sub crossover overrides must be an object keyed by side or group")
        keys = bass_override_keys(mode)
        for key, definition in sub_filters.items():
            if key not in keys:
                raise ValueError(f"Sub crossover overrides must be keyed by {', '.join(keys)}")
            if definition is None:
                bass["sub_filters"].pop(key, None)
            else:
                bass["sub_filters"][key] = definition
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
    active = derive_topology(mode, routing_for_device(result, mode, output_key, channels), channels=channels,
                             crossover_enabled=effective_crossover_enabled(result, mode, channels))
    if pure_stereo_roles(mode, active.roles) and bank_id != "global":
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
