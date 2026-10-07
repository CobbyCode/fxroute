# SPDX-License-Identifier: AGPL-3.0-only
"""Surround Global presets: PEQ/convolver plugins scoped to a speaker group.

A Surround Global preset is one preset whose plugins each carry the speaker
group they correct (front, center, side, rear). The native engine runs a
tagged plugin only on the effect pairs of its group, ahead of the matrix and
the bass-management split, so the correction covers the speaker and its share
of the sub bus. LFE is no speaker group: no plugin targets it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

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


def validate_preset_layout(payload: dict) -> None:
    """Reject group tags without the Surround layout, and invalid Surround chains."""
    chain = payload.get("chain") if isinstance(payload.get("chain"), list) else []
    if preset_layout(payload) == LAYOUT:
        validate_surround_chain(chain, preset="(new)")
    elif any(isinstance(plugin, dict) and plugin.get("group") for plugin in chain):
        raise ValueError("Plugin speaker groups require the Surround preset layout")


def routed_chain(chain: Sequence[dict], routed: frozenset[str]) -> list[dict]:
    """Plugins whose group has a routed speaker, in chain order.

    A group without its own speaker folds down into another one; its filter
    was measured on that missing speaker and must not shape the fold-down.
    """
    return [plugin for plugin in chain if plugin.get("group") in routed or not plugin.get("enabled", True)]


def pair_mask(plugin: dict) -> int | None:
    """Engine effect-pair mask of a tagged plugin (None means all pairs)."""
    group = plugin.get("group")
    return GROUP_PAIR_MASK[group] if group else None
