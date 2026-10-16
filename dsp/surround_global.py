# SPDX-License-Identifier: AGPL-3.0-only
"""Surround Global: one PEQ/convolver preset per speaker group in each A/B slot.

A Surround Global preset holds the filters of one speaker group (front,
center, side, rear); every plugin carries that group. A Global A/B slot is a
group selection that names one preset (or Neutral) per group, and the filters
of all routed groups run at the same time. The native engine runs a tagged
plugin only on the effect pairs of its group, ahead of the matrix and the
bass-management split, so the correction covers the speaker and its share of
the sub bus. LFE is no speaker group: no plugin targets it.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Sequence

from audio.output_topology import SUB_ROLES, SURROUND_GROUPS, surround_group

LAYOUT = "surround"
GROUP_PLUGIN_TYPES = ("equalizer", "convolver")
BUILTIN_PRESETS = ("Direct", "Neutral")
# Engine effect pairs (native_dsp/dsp.c): FL/FR 0, FC 1, LFE 2, SL/SR 3, BL/BR 4.
GROUP_PAIR_MASK = {"front": 0b00001, "center": 0b00010, "side": 0b01000, "rear": 0b10000}

assert tuple(GROUP_PAIR_MASK) == SURROUND_GROUPS


def preset_layout(payload: object) -> str | None:
    """Layout tag of a preset payload (None for stereo/legacy presets)."""
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    layout = metadata.get("layout") if isinstance(metadata, dict) else None
    return layout if isinstance(layout, str) and layout else None


def routed_groups(roles: Iterable[str]) -> frozenset[str]:
    """Speaker groups that have their own routed speaker (subs never count)."""
    return frozenset(surround_group(role) for role in roles if role not in SUB_ROLES)


def _delay_band(plugin: dict) -> bool:
    params = plugin.get("params") if isinstance(plugin.get("params"), dict) else {}
    for key in ("bands", "leftBands", "rightBands"):
        for band in params.get(key) or []:
            if isinstance(band, dict) and band.get("filterType") == "delay" and band.get("enabled", True):
                return True
    return False


def validate_surround_chain(chain: Sequence[dict], *, preset: str) -> None:
    """Every enabled plugin must be a group-tagged PEQ or convolver."""
    for plugin in chain:
        if not plugin.get("enabled", True):
            continue
        label = f'Surround preset "{preset}" plugin {plugin.get("id", plugin.get("type"))}'
        if plugin.get("type") not in GROUP_PLUGIN_TYPES:
            raise ValueError(f"{label}: Surround Global supports only PEQ and convolver plugins")
        if plugin.get("group") not in GROUP_PAIR_MASK:
            raise ValueError(f"{label} needs a speaker group ({', '.join(SURROUND_GROUPS)})")
        if plugin["type"] == "equalizer" and _delay_band(plugin):
            raise ValueError(f"{label}: a delay band would shift every speaker group")
    if len({plugin.get("group") for plugin in chain if plugin.get("enabled", True)}) > 1:
        raise ValueError(f'Surround preset "{preset}" mixes speaker groups; a preset holds the filters of one group')


def validate_preset_layout(payload: dict) -> None:
    """Reject group tags without the Surround layout, and invalid Surround chains."""
    chain = payload.get("chain") if isinstance(payload.get("chain"), list) else []
    if preset_layout(payload) == LAYOUT:
        validate_surround_chain(chain, preset="(new)")
    elif any(isinstance(plugin, dict) and plugin.get("group") for plugin in chain):
        raise ValueError("Plugin speaker groups require the Surround preset layout")


def neutral_selection() -> dict[str, str]:
    """Group selection without any filter."""
    return {group: "Neutral" for group in SURROUND_GROUPS}


def preset_group(payload: object) -> str | None:
    """Speaker group of a Surround preset (None without plugins or layout)."""
    chain = payload.get("chain") if isinstance(payload, dict) and preset_layout(payload) == LAYOUT else None
    groups = {plugin.get("group") for plugin in chain or [] if isinstance(plugin, dict) and plugin.get("enabled", True)}
    return next(iter(groups)) if len(groups) == 1 and None not in groups else None


def selection_label(selection: dict) -> str:
    """Short text of a group selection, e.g. "front: Room FIR · rear: Rear PEQ"."""
    chosen = [f"{group}: {selection[group]}" for group in SURROUND_GROUPS if selection.get(group, "Neutral") != "Neutral"]
    return " · ".join(chosen) or "Neutral"


def selection_chain(selection: dict, routed: frozenset[str], load: Callable[[str], dict]) -> list[dict]:
    """Global chain of a group selection: the preset of every routed group, in group order.

    ``load`` reads a preset of the Surround Global library. A group without a
    routed speaker folds down into another one; its filter was measured on
    that missing speaker and must not shape the fold-down. Plugin ids are
    renumbered per type, as Combine numbers them.
    """
    chain: list[dict] = []
    for group in SURROUND_GROUPS:
        name = selection.get(group, "Neutral")
        if group not in routed or name == "Neutral":
            continue
        payload = load(name)
        if preset_layout(payload) != LAYOUT:
            raise ValueError(f'Preset "{name}" is no Surround Global preset')
        validate_surround_chain(payload["chain"], preset=name)
        if preset_group(payload) not in (None, group):
            raise ValueError(f'Preset "{name}" holds {preset_group(payload)} filters, not {group} filters')
        chain.extend(copy.deepcopy(plugin) for plugin in payload["chain"])
    counters: dict[str, int] = {}
    for plugin in chain:
        number = counters.get(plugin["type"], 0)
        counters[plugin["type"]] = number + 1
        plugin["id"] = f"{plugin['type']}#{number}"
    return chain
