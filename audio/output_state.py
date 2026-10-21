# SPDX-License-Identifier: AGPL-3.0-only
"""Versioned per-mode routing, correction banks, and output processing state."""

from __future__ import annotations

import copy
import math

from audio.output_routing import device_key
from audio import hdmi
from audio.filter_banks import bank_definitions, owning_bank_ids, resolve_bank, summarize_banks
from audio.output_topology import (CENTER_ROLES, MAIN_ROLES, MODES, SURROUND_GROUPS, derive_topology, dormant_roles,
                                   roles_for_mode, validate_assignments)
from dsp.banks import BankState, slot_presets
from dsp.latency import default_av_sync, validate_av_sync
from dsp.surround_global import neutral_selection

SCHEMA = "fxroute.output-state"
VERSION = 8
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


def default_global_bank(mode: str) -> dict:
    """Global bank without filters; Surround selects a preset per speaker group."""
    if mode == "surround":
        return BankState(neutral_selection(), neutral_selection(), neutral_selection()).to_dict()
    return BankState().to_dict()


def default_output_state() -> dict:
    modes = {}
    for mode in MODES:
        roles = MAIN_ROLES
        modes[mode] = {
            "routing": {}, "crossover_enabled": False, "device_crossovers": {}, "selected_bank": "global",
            "banks": {"global": default_global_bank(mode), **{role: BankState().to_dict() for role in roles}},
            "processing": {role: default_processing() for role in roles},
            "bass_management": default_bass_management(),
            "extras": {},
            "av_sync": default_av_sync(),
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


def _validate_slot_kinds(mode: str, role: str, binding: dict) -> None:
    """Surround Global slots select one preset per speaker group (or Direct); others name one preset."""
    group_slots = mode == "surround" and role == "global"
    for slot in (binding["preset"], binding["preset_a"], binding["preset_b"]):
        if slot is None:
            continue
        if group_slots and slot != "Direct" and (not isinstance(slot, dict) or set(slot) != set(SURROUND_GROUPS)):
            raise ValueError("Surround Global slots select one preset per speaker group")
        if not group_slots and isinstance(slot, dict):
            raise ValueError("Only Surround Global slots select presets per speaker group")


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
        _object_fields(config, {"routing", "crossover_enabled", "device_crossovers", "selected_bank", "banks", "processing", "bass_management", "extras", "av_sync"}, "Mode state")
        enabled = config["crossover_enabled"]
        roles_for_mode(mode, crossover_enabled=enabled)
        crossovers = config["device_crossovers"]
        if not isinstance(crossovers, dict):
            raise ValueError("Device crossovers must be an object keyed by device")
        for key, value in crossovers.items():
            if hdmi.routing_key(_device_identity(key)) != key or type(value) is not bool:
                raise ValueError("Device crossovers require normalized device keys and booleans")
        # Dormant banks retain their filters when crossover is toggled.
        allowed = (*roles_for_mode(mode), *roles_for_mode(mode, crossover_enabled=True))
        banks, processing, routing = config["banks"], config["processing"], config["routing"]
        if not isinstance(banks, dict) or "global" not in banks:
            raise ValueError("Every mode requires a Global bank")
        if any(role not in ("global", *allowed) for role in banks):
            raise ValueError("Bank role is not valid for its output mode")
        for role, bank in banks.items():
            BankState.from_dict(bank)
            _validate_slot_kinds(mode, role, bank)
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
            roles = validate_assignments(mode, assignments, crossover_enabled=effective_crossover_enabled(
                payload, mode, output_key=key), allow_dormant=True)
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
        validate_av_sync(config['av_sync'], allowed, surround=mode == 'surround')
    return copy.deepcopy(payload)


def set_av_sync(state: dict, mode: str, *, target_ms: object) -> dict:
    result = validate_output_state(state)
    if mode not in MODES:
        raise ValueError('Unknown AV Sync output mode')
    result['modes'][mode]['av_sync']['target_ms'] = target_ms
    return validate_output_state(result)


def routing_for_device(state: dict, mode: str, output_key: str, channels: int | None = None) -> list[str]:
    """Active routing, or the fixed stereo mapping on two-output devices.

    A non-HDMI device with exactly two hardware outputs plays Out 1 = Main L
    and Out 2 = Main R. HDMI retains its configured receiver-channel mapping.
    Passing the effective channel count returns that fixed mapping so every
    caller renders the same topology. It also bounds a migrated routing list to
    the device's real outputs; wider stored ways stay for a wider device state.

    Crossover-path assignments outside the mode's current domain are dormant
    and read as Off here, so with the crossover off the stored ways never
    reach the plan, the meters or the routing grid.
    """
    roles_for_mode(mode)
    count = None
    if channels is not None:
        try:
            count = int(channels)
        except (TypeError, ValueError):
            count = None
        if count == 2 and not hdmi.is_hdmi_key(output_key):
            return list(MAIN_ROLES)
    config = state["modes"][mode]
    enabled = effective_crossover_enabled(state, mode, output_key=output_key)
    fallback = [] if enabled else list(MAIN_ROLES)
    saved = hdmi.stored_routing(config["routing"], _device_identity(output_key))
    stored = list(saved if saved is not None else fallback)
    if count is not None and count > 0:
        stored = stored[:count]
    dormant = set(dormant_roles(mode, crossover_enabled=enabled))
    return ["off" if role in dormant else role for role in stored]


def effective_crossover_enabled(state: dict, mode: str, channels: int | None = None, *,
                                 output_key: str | None = None) -> bool:
    """Device crossover selection, or legacy mode intent, off on two outputs.

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
    config = state["modes"][mode]
    key = hdmi.routing_key(_device_identity(output_key)) if output_key is not None else None
    return config["device_crossovers"].get(key, config["crossover_enabled"])


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
    enabled = effective_crossover_enabled(result, mode, output_key=output_key)
    values = list(validate_assignments(mode, assignments, crossover_enabled=enabled))
    count = len(values)
    key = hdmi.routing_key(_device_identity(output_key))
    config = result["modes"][mode]
    config["device_crossovers"][key] = enabled
    stored = hdmi.stored_routing(config["routing"], _device_identity(output_key)) or []
    dormant = set(dormant_roles(mode, crossover_enabled=enabled))
    values = [stored[index] if (value == "off" and index < len(stored) and stored[index] in dormant)
              else value for index, value in enumerate(values)]
    values.extend(stored[count:])
    config["routing"][key] = values
    for role in values:
        if role != "off":
            config["banks"].setdefault(role, BankState().to_dict())
            config["processing"].setdefault(role, default_processing())
    masked = ["off" if value in dormant else value for value in values]
    topology = derive_topology(mode, masked, channels=count, crossover_enabled=enabled)
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


def seed_device_routing(state: dict, output_key: str, assignments_by_mode: dict, *,
                        capacity: int | None = None) -> dict:
    """Store default routing for the modes that have none for this device yet.

    A stored routing, edited or not, is never replaced, and a mode with the
    crossover on keeps the existing no-default behavior. A migrated legacy
    routing is limited to ``capacity``, the device's real output count.
    """
    result = validate_output_state(state)
    key = hdmi.routing_key(_device_identity(output_key))
    for mode, assignments in assignments_by_mode.items():
        config = result["modes"][mode]
        saved = hdmi.stored_routing(config["routing"], output_key)
        if key in config["routing"]:
            continue
        if saved is not None:
            config["routing"][key] = list(saved[:capacity] if capacity is not None and capacity > 0 else saved)
        elif assignments and not effective_crossover_enabled(result, mode, output_key=output_key):
            result = set_mode_routing(result, mode, output_key, assignments)
    return result


def switch_mode(state: dict, mode: str) -> dict:
    roles_for_mode(mode)
    result = validate_output_state(state)
    result["active_mode"] = mode
    return result


def set_crossover(state: dict, mode: str, enabled: bool, *, output_key: str | None = None) -> dict:
    """Change the role domain in the same routing, retaining dormant DSP banks.

    Main ports become Low ports on enable. On disable only Low ports become
    Main; the higher ways stay stored as dormant crossover-path assignments,
    so switching the crossover back on restores the same ways on the same
    ports. No second crossover routing is stored.
    """
    roles_for_mode(mode, crossover_enabled=enabled)
    result = validate_output_state(state)
    config = result["modes"][mode]
    if effective_crossover_enabled(result, mode, output_key=output_key) == enabled:
        if output_key is not None:
            config["device_crossovers"][hdmi.routing_key(_device_identity(output_key))] = enabled
        return result
    mapping = {"main_l": "left_low", "main_r": "right_low"} if enabled else {
        "left_low": "main_l", "right_low": "main_r"}
    allowed = (*roles_for_mode(mode, crossover_enabled=enabled), "off")
    domain = (*roles_for_mode(mode), *roles_for_mode(mode, crossover_enabled=True), "off")
    if output_key is None:
        config["crossover_enabled"] = enabled
        config["device_crossovers"] = {key: enabled for key in config["device_crossovers"]}
    else:
        config["device_crossovers"][hdmi.routing_key(_device_identity(output_key))] = enabled
    for key, assignments in config["routing"].items():
        if output_key is not None and hdmi.routing_key(key) != hdmi.routing_key(_device_identity(output_key)):
            continue
        device_mapping = dict(mapping)
        # A full-range Center stays full-range unless dormant ways restore its split.
        # A Low-only draft stays dormant rather than becoming a full-range speaker.
        if mode == "surround" and any(role in CENTER_ROLES[1:] for role in assignments):
            if enabled and "center_low" not in assignments:
                device_mapping["center"] = "center_low"
            elif not enabled and "center" not in assignments:
                device_mapping["center_low"] = "center"
        config["routing"][key] = [device_mapping.get(role, role if role in domain else "off") for role in assignments]
        for role in config["routing"][key]:
            if role != "off":
                config["banks"].setdefault(role, BankState().to_dict())
                config["processing"].setdefault(role, default_processing())
    if config["selected_bank"] not in ("global", "all", *allowed, *bank_definitions(allowed)):
        config["selected_bank"] = "all" if mode == "surround" else "global"
    return validate_output_state(result)


_UNCHANGED: object = object()


def _group_slot(slot: object, group: str | None, name: str) -> str | dict:
    """Surround Global slot with ``name`` selected for ``group``; Direct replaces the selection."""
    if name == "Direct":
        return name
    if group not in SURROUND_GROUPS:
        raise ValueError("Surround Global selects a preset per speaker group; name the group")
    selection = dict(slot) if isinstance(slot, dict) else neutral_selection()
    selection[group] = name
    return selection


def set_bank_preset(state: dict, mode: str, bank_id: str, *, preset: str | None = None,
                    preset_a: str | None = None, preset_b: object = _UNCHANGED,
                    active_side: str | None = None, roles=None, group: str | None = None) -> dict:
    """Assign bank presets without touching routing or other banks.

    ``preset`` writes the currently listened slot (slot A when the bank
    listens to neither slot yet) and listens to it. ``active_side`` alone
    switches listening; an unassigned B side is rejected. Slot arguments
    set compare slots directly; an explicit ``preset_b=None`` clears B.

    Surround Global selects one preset per speaker group: ``group`` names
    the group a preset replaces in its slot, the other groups keep theirs.
    An unassigned B starts from A's selection, and editing the listened slot
    keeps listening to it.
    """
    result = validate_output_state(state)
    roles_for_mode(mode)
    banks = result["modes"][mode]["banks"]
    members = resolve_bank(result["modes"][mode], bank_id, roles)["roles"]
    if active_side is not None and active_side not in {"A", "B"}:
        raise ValueError("Compare side must be A or B")
    slot = active_side or summarize_banks([banks[role] for role in members])["active_side"] or "A"
    if mode == "surround" and members == ["global"]:
        current = banks["global"]
        listened = BankState.from_dict(current).active_side
        if preset is not None:
            preset = _group_slot(current[f"preset_{slot.lower()}"], group, preset)
        if preset_a is not None:
            preset_a = _group_slot(current["preset_a"], group, preset_a)
            active_side = active_side or ("A" if listened == "A" else None)
        if preset_b is not _UNCHANGED and preset_b is not None:
            preset_b = _group_slot(current["preset_b"] or current["preset_a"], group, preset_b)
            active_side = active_side or ("B" if listened == "B" else None)
    elif group is not None:
        raise ValueError("A speaker group applies to the Surround Global bank only")
    for role in members:
        current = dict(banks[role])
        listened = BankState.from_dict(current).active_side
        current.pop("active_side", None)
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
        bank = BankState.from_dict(current)
        if preset is not None or active_side is not None:
            bank = bank.select(slot)
        elif listened == "B" and bank.preset == bank.preset_b:
            bank = bank.select("B")
        banks[role] = bank.to_dict()
    return validate_output_state(result)


def switch_all_banks(state: dict, mode: str, output_key: str, channels: int, active_side: str) -> dict:
    """Switch configured area bindings atomically, excluding Global and dormant roles.

    B needs a B slot in at least one bank; banks without one stay on A.
    """
    result = validate_output_state(state)
    config = result["modes"][mode]
    topology = derive_topology(mode, routing_for_device(result, mode, output_key, channels), channels=channels,
                               crossover_enabled=effective_crossover_enabled(result, mode, channels, output_key=output_key))
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
                             crossover_enabled=effective_crossover_enabled(result, mode, channels, output_key=output_key))
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


def _uses_library(slot_mode: str, role: str, mode: str | None, bank_id: str) -> bool:
    """Whether a role's slots name presets of one bank's library.

    Global has a library per output mode; an area bank's library serves its
    roles in every mode.
    """
    if bank_id == "global":
        return role == "global" and slot_mode == mode
    return bank_id in owning_bank_ids(role)


def release_bank_preset(state: dict, mode: str | None, bank_id: str, name: str) -> tuple[dict, list[str]]:
    """Clear every slot holding one preset of one bank's library.

    The compare rules for a preset that goes away: a released A falls back
    to Neutral, a released B is cleared, and a role listening to a released
    slot falls back to A. A Surround Global selection releases only the
    group holding the preset; equal A/B selections stay assigned. Returns the
    new state and one line per touched role.
    """
    result = validate_output_state(state)
    changes = []

    def released(slot):
        if isinstance(slot, dict):
            return {group: "Neutral" if preset == name else preset for group, preset in slot.items()}
        return "Neutral" if slot == name else slot

    for slot_mode, config in result["modes"].items():
        for role, binding in config["banks"].items():
            if (not _uses_library(slot_mode, role, mode, bank_id)
                    or name not in {preset for key in ("preset", "preset_a", "preset_b")
                                    for preset in slot_presets(binding[key])}):
                continue
            side = BankState.from_dict(binding).active_side
            preset_a = released(binding["preset_a"])
            preset_b = released(binding["preset_b"]) if isinstance(binding["preset_b"], dict) else binding["preset_b"]
            if preset_b == name:
                preset_b = None
            if side == "B" and preset_b is not None:
                preset = preset_b
            elif side is None and name not in slot_presets(binding["preset"]):
                preset = binding["preset"]
            else:
                preset = preset_a
            bank = BankState(preset, preset_a, preset_b)
            if side == "B" and preset_b is not None:
                bank = bank.select("B")
            config["banks"][role] = bank.to_dict()
            changes.append(f"{slot_mode}/{role}: released {name!r}; "
                           f"A={preset_a!r} B={preset_b!r} listening={preset!r}")
    return validate_output_state(result), changes


def referenced_presets(state: dict, mode: str | None, bank_id: str) -> set[str]:
    """Preset names the slots of one bank's library hold."""
    validated = validate_output_state(state)
    return {name for slot_mode, config in validated["modes"].items()
            for role, bank in config["banks"].items() if _uses_library(slot_mode, role, mode, bank_id)
             for key in ("preset", "preset_a", "preset_b") for name in slot_presets(bank[key])}
