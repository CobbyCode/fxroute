# SPDX-License-Identifier: AGPL-3.0-only
"""Logical stereo/mono banks over losslessly retained per-role preset bindings.

Bank identity is shared by comparison, import and measurement. Role bindings
are the storage/native projection, not independent editing targets. Keeping
them preserves older installations with different left and right presets.
"""

from __future__ import annotations

from dsp.banks import BankState

STEREO_PAIRS = {
    "main": ("main_l", "main_r"),
    "low": ("left_low", "right_low"),
    "low_mid": ("left_low_mid", "right_low_mid"),
    "mid": ("left_mid", "right_mid"),
    "high": ("left_high", "right_high"),
    "sub": ("sub_l", "sub_r"),
}


def bank_label(bank_id: str) -> str:
    if bank_id in STEREO_PAIRS:
        return f"{bank_id.replace('_', '-').title()} L/R"
    if bank_id.startswith("sub") and bank_id[3:].isdigit():
        return f"Sub {bank_id[3:]}"
    return bank_id.replace("_", " ").title()


def bank_definitions(roles) -> dict[str, dict]:
    """Group complete pairs; fanout and mono roles never create extra banks."""
    active = set(roles) - {"off", "global"}
    definitions = {}
    for bank_id, members in STEREO_PAIRS.items():
        if set(members) <= active:
            definitions[bank_id] = {"id": bank_id, "label": bank_label(bank_id),
                                    "roles": list(members), "channel_mode": "stereo"}
            active.difference_update(members)
    for role in dict.fromkeys(roles):
        if role in active:
            definitions[role] = {"id": role, "label": bank_label(role),
                                 "roles": [role], "channel_mode": "mono"}
    return definitions


def owning_bank_ids(role: str) -> set[str]:
    """Bank ids a role can own presets under in any topology.

    A pair role resolves to its pair bank, or to itself when routed alone.
    """
    if role == "global":
        return {"global"}
    return {role} | {bank_id for bank_id, members in STEREO_PAIRS.items() if role in members}


def resolve_bank(config: dict, bank_id: str, roles=None) -> dict:
    """Resolve a logical bank or an old role selection to its whole group."""
    if bank_id == "global":
        return {"id": "global", "label": "Global", "roles": ["global"], "channel_mode": "stereo"}
    definitions = bank_definitions(config["banks"] if roles is None else roles)
    for definition in definitions.values():
        if bank_id == definition["id"] or bank_id in definition["roles"]:
            if all(role in config["banks"] for role in definition["roles"]):
                return definition
    raise ValueError(f"Unknown or unconfigured filter bank: {bank_id}")


def summarize_banks(bindings: list[dict]) -> dict:
    """Aggregate compare slots without inventing a preset for mixed bindings."""
    def common(key):
        values = {binding[key] for binding in bindings}
        return next(iter(values)) if len(values) == 1 else None

    sides = {BankState.from_dict(binding).active_side for binding in bindings}
    return {"preset": common("preset"), "preset_a": common("preset_a"),
            "preset_b": common("preset_b"),
            "active_side": next(iter(sides)) if len(sides) == 1 else None,
            "can_a": bool(bindings),
            "can_b": bool(bindings) and all(binding["preset_b"] is not None for binding in bindings)}


def summarize_all_banks(bindings: list[dict]) -> dict:
    """All Banks aggregate: B needs one bank with a B; banks without B stay on A.

    The aggregate listens to B while every bank with a B does and every bank
    without one sits on A, so the joint A/B switch reads as B after it.
    """
    comparable = [binding for binding in bindings if binding["preset_b"] is not None]
    fixed_on_a = all(BankState.from_dict(binding).active_side == "A"
                     for binding in bindings if binding["preset_b"] is None)
    sides = summarize_banks(comparable if comparable and fixed_on_a else bindings)["active_side"]
    return {**summarize_banks(bindings), "active_side": sides, "can_b": bool(comparable)}


def bank_catalog(config: dict, roles) -> dict:
    """Project only configured banks, with topology-derived channel metadata."""
    definitions = {"global": resolve_bank(config, "global"), **bank_definitions(roles)}
    return {bank_id: {**definition, **summarize_banks([config["banks"][role] for role in definition["roles"]])}
            for bank_id, definition in definitions.items()}


def selected_bank(config: dict, roles) -> str:
    """Pure stereo keeps the original single global A/B surface."""
    roles = tuple(roles)
    if set(roles) == {"main_l", "main_r"}:
        return "global"
    if config["selected_bank"] == "all" and roles:
        return "all"
    try:
        return resolve_bank(config, config["selected_bank"], roles)["id"]
    except ValueError:
        return "global"
