# SPDX-License-Identifier: AGPL-3.0-only
"""Speaker positions of the footer's multichannel output meter.

The native engine exposes one output per logical role (``main_l``, ``center``,
``left_high`` …).  The footer meter shows speakers, not amplifier channels, so
roles that feed the same speaker position share one meter: the ways of a
crossover side meter as that side, and the two subs share one bass meter.
Every layout therefore reduces to at most eight positions
(FL FR C LFE SL SR RL RR).
"""

from __future__ import annotations

from dataclasses import dataclass

# Fewer positions than this keep the stereo meter.
MIN_MULTICHANNEL_POSITIONS = 3

_SURROUND_ORDER = ("fl", "fr", "c", "lfe", "sl", "sr", "rl", "rr")
_STEREO_ORDER = ("l", "r", "sub_l", "sub_r", "sub")

_SURROUND_KEYS = {
    "main_l": "fl", "main_r": "fr", "center": "c", "side_l": "sl", "side_r": "sr",
    "rear_l": "rl", "rear_r": "rr", "sub1": "lfe", "sub2": "lfe",
}
_STEREO_KEYS = {
    "main_l": "l", "main_r": "r", "sub_l": "sub_l", "sub_r": "sub_r",
    "sub1": "sub", "sub2": "sub",
}


@dataclass(frozen=True)
class MeterPosition:
    """One meter column and the engine outputs (zero-based) it follows."""

    key: str
    outputs: tuple[int, ...]


def _position_key(mode: str, role: str) -> str | None:
    surround = mode == "surround"
    if role.startswith("left_"):
        return "fl" if surround else "l"
    if role.startswith("right_"):
        return "fr" if surround else "r"
    return (_SURROUND_KEYS if surround else _STEREO_KEYS).get(role)


def meter_positions(mode: str, roles: tuple[str, ...]) -> tuple[MeterPosition, ...]:
    """Ordered meter positions for an engine layout, or none for an unknown role."""
    order = _SURROUND_ORDER if mode == "surround" else _STEREO_ORDER
    outputs: dict[str, list[int]] = {}
    for index, role in enumerate(roles):
        key = _position_key(mode, str(role))
        if key is None:
            return ()
        outputs.setdefault(key, []).append(index)
    return tuple(MeterPosition(key, tuple(outputs[key])) for key in order if key in outputs)
