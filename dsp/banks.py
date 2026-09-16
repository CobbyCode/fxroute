# SPDX-License-Identifier: AGPL-3.0-only
"""Independent correction-bank A/B selections over a shared preset library."""

from __future__ import annotations

from dataclasses import dataclass, replace


def _validate_name(value: object) -> None:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or value in {".", ".."} or any(char in value for char in ("/", "\\"))
            or any(ord(char) < 32 for char in value)):
        raise ValueError("Preset name must be a non-empty filename without control characters")


@dataclass(frozen=True)
class BankState:
    preset: str = "Neutral"
    preset_a: str = "Neutral"
    preset_b: str | None = None

    def __post_init__(self) -> None:
        _validate_name(self.preset)
        _validate_name(self.preset_a)
        if self.preset_b is not None:
            _validate_name(self.preset_b)
            if self.preset_b == self.preset_a:
                raise ValueError("Compare slots must use distinct presets")

    @property
    def active_side(self) -> str | None:
        return "A" if self.preset == self.preset_a else "B" if self.preset == self.preset_b else None

    def select(self, side: str) -> BankState:
        if side not in {"A", "B"}:
            raise ValueError("Compare side must be A or B")
        name = self.preset_a if side == "A" else self.preset_b
        if name is None:
            raise ValueError("Compare side has no assigned preset")
        return replace(self, preset=name)

    def to_dict(self) -> dict:
        return {"preset": self.preset, "preset_a": self.preset_a, "preset_b": self.preset_b}

    @classmethod
    def from_dict(cls, payload: object) -> BankState:
        if not isinstance(payload, dict) or set(payload) != {"preset", "preset_a", "preset_b"}:
            raise ValueError("Bank requires preset, preset_a, and preset_b")
        return cls(**payload)
