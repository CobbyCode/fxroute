# SPDX-License-Identifier: AGPL-3.0-only
"""A/B selection of one correction bank over that bank's own preset library.

A slot names one preset. Surround Global slots instead select one preset per
speaker group (``{"front": name, ...}``); the output state checks the groups.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


def _validate_name(value: object) -> None:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or value in {".", ".."} or any(char in value for char in ("/", "\\"))
            or any(ord(char) < 32 for char in value)):
        raise ValueError("Preset name must be a non-empty filename without control characters")


def _validate_slot(value: object) -> None:
    if not isinstance(value, dict):
        _validate_name(value)
        return
    if not value or any(not isinstance(group, str) for group in value):
        raise ValueError("A group selection maps speaker groups to presets")
    for name in value.values():
        _validate_name(name)
        if name == "Direct":
            raise ValueError("Direct bypasses every speaker group; it is no group selection")


def slot_presets(slot: object) -> tuple[str, ...]:
    """Preset names one slot holds (every group of a group selection)."""
    if slot is None:
        return ()
    return tuple(slot.values()) if isinstance(slot, dict) else (slot,)


@dataclass(frozen=True)
class BankState:
    preset: str | dict = "Neutral"
    preset_a: str | dict = "Neutral"
    preset_b: str | dict | None = "Neutral"
    _side: str | None = None

    def __post_init__(self) -> None:
        _validate_slot(self.preset)
        _validate_slot(self.preset_a)
        if self.preset_b is not None:
            _validate_slot(self.preset_b)
        if self._side is not None:
            if self._side not in ("A", "B") or self.preset != (self.preset_a if self._side == "A" else self.preset_b):
                raise ValueError("Stored compare side must match the listened preset")

    @property
    def active_side(self) -> str | None:
        if self._side is not None:
            return self._side
        return "A" if self.preset == self.preset_a else "B" if self.preset == self.preset_b else None

    def select(self, side: str) -> BankState:
        if side not in {"A", "B"}:
            raise ValueError("Compare side must be A or B")
        name = self.preset_a if side == "A" else self.preset_b
        if name is None:
            raise ValueError(f"Compare side {side} has no assigned preset")
        # Only equal slots need a persisted tie-break; other bindings keep their shape.
        return replace(self, preset=name, _side="B" if side == "B" and self.preset_a == self.preset_b else None)

    def to_dict(self) -> dict:
        def copy(slot):
            return dict(slot) if isinstance(slot, dict) else slot
        return {"preset": copy(self.preset), "preset_a": copy(self.preset_a), "preset_b": copy(self.preset_b),
                **({"active_side": "B"} if self.preset_a == self.preset_b and self.active_side == "B" else {})}

    @classmethod
    def from_dict(cls, payload: object) -> BankState:
        fields = {"preset", "preset_a", "preset_b"}
        if not isinstance(payload, dict) or set(payload) not in (fields, fields | {"active_side"}):
            raise ValueError("Bank requires preset, preset_a, and preset_b")
        return cls(**{key: payload[key] for key in fields}, _side=payload.get("active_side"))
