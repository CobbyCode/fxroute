# SPDX-License-Identifier: AGPL-3.0-only
"""Speaker Level Matching: scope planning, stored raw levels and cut-only trims.

Every take measures the level reference (``normalized_by_db``) of one speaker.
The raw level of a role is that measurement minus the Level Calibration slot
the role had while it was measured, so a re-measurement overwrites its own slot
without counting it twice.  The calibration is cut-only: all roles of one
family share a target ``T`` equal to the quietest raw level, and each role's
trim is ``T - raw`` (never above 0 dB).  The per-output gain sits behind the
protection limiter, so a positive trim could clip.

Families: Stereo scopes (Global or one bank) match their L/R roles among each
other; Surround has one family over all speaker groups, where Front L/R
establishes the reference.  Raw levels are stored, not the trims, so a later,
quieter speaker lowers ``T`` and every stored trim follows from the table.

This module is pure apart from ``LevelMatchStore``: no hardware, no runtime.
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path

from audio.output_topology import SUB_ROLES, surround_group
from common.atomic_write import atomic_write_text
from measurement.speaker_scope import plan_area

SCHEMA = "fxroute.level-match"
VERSION = 1
SURROUND_FAMILY = "surround"
# Front L/R is the Surround reference and the only group written to the
# "global" slot; the other speakers sit in their own bank slot.
SURROUND_GROUP_SLOT = {"front": "global", "center": "bank", "side": "bank", "rear": "bank"}
# A spread this large between two speakers points to a failed capture (unplugged
# or misplaced microphone, wrong routing), not to a level difference to trim.
MAX_PLAUSIBLE_SPREAD_DB = 24.0
TRIM_RESOLUTION_DB = 0.1
VOLUME_TOLERANCE_PERCENT = 0.5
FINGERPRINT_KEYS = ("device_key", "sample_rate_hz", "mic_node", "mic_channel",
                    "capture_volume_percent", "system_volume_percent")


def _finite(value: object, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"Level Matching {label} must be finite")
    return float(value)


def plan_scope(mode: str, bank_id: str, roles: Sequence[str]) -> dict:
    """Resolve what a Level Matching run on one area measures.

    Returns ``family``, ``slot``, ``requires_reference`` and ``units`` (see
    ``speaker_scope.plan_area``; subwoofers are never measured).
    """
    area = plan_area(mode, bank_id, roles)
    if mode == "surround":
        group = area["group"]
        return {"family": SURROUND_FAMILY, "slot": SURROUND_GROUP_SLOT[group],
                "requires_reference": group != "front", "group": group, "units": area["units"]}
    slot, family = ("global", "scope:global") if bank_id == "global" else ("bank", f"scope:{bank_id}")
    if len(area["units"]) < 2:
        raise ValueError("Level Matching needs a left/right speaker pair")
    return {"family": family, "slot": slot, "requires_reference": False, "group": None,
            "units": area["units"]}


def make_fingerprint(*, device_key: str, sample_rate_hz: int, mic_node: str, mic_channel: object,
                     capture_volume_percent: float | None,
                     system_volume_percent: float | None) -> dict:
    return {"device_key": device_key, "sample_rate_hz": sample_rate_hz, "mic_node": mic_node,
            "mic_channel": str(mic_channel), "capture_volume_percent": capture_volume_percent,
            "system_volume_percent": system_volume_percent}


def fingerprints_match(first: Mapping, second: Mapping) -> bool:
    """Whether two takes were captured through the same device, rate, mic and gain."""
    for key in FINGERPRINT_KEYS:
        left, right = first.get(key), second.get(key)
        if key.endswith("_percent"):
            if left is None or right is None:
                if left is not right:
                    return False
            elif abs(float(left) - float(right)) > VOLUME_TOLERANCE_PERCENT:
                return False
        elif left != right:
            return False
    return True


def raw_level_db(normalized_by_db: object, own_slot_db: Sequence[float]) -> float:
    """Level reference of a take with the role's own Level Calibration slot removed."""
    measured = _finite(normalized_by_db, "measured level")
    slots = [_finite(value, "slot") for value in own_slot_db]
    return round(measured - (sum(slots) / len(slots) if slots else 0.0), 3)


def entry_key(role: str, slot: str) -> str:
    """A role can be measured under both slots (Global and its bank), so the slot is part of the key."""
    return f"{slot}:{role}"


def make_entry(*, role: str, raw_db: float, slot: str, family: str, fingerprint: Mapping,
               measured_at: str) -> dict:
    return {"role": role, "raw_db": _finite(raw_db, "raw level"), "slot": slot, "family": family,
            "fingerprint": dict(fingerprint), "measured_at": measured_at}


def valid_entries(entries: Mapping[str, dict]) -> dict[str, dict]:
    """Entries that share one capture chain with the newest entry of their family.

    Entries from another device, rate, microphone or gain are "outdated": they
    stay in the table (and keep their applied trim) but no longer take part in
    the target, because their raw levels are not comparable.
    """
    anchors: dict[str, dict] = {}
    for entry in entries.values():
        anchor = anchors.get(entry["family"])
        if anchor is None or entry["measured_at"] > anchor["measured_at"]:
            anchors[entry["family"]] = entry
    return {role: entry for role, entry in entries.items()
            if fingerprints_match(entry["fingerprint"], anchors[entry["family"]]["fingerprint"])}


def has_front_reference(entries: Mapping[str, dict], front_roles: Sequence[str]) -> bool:
    """Both Front sides are present and comparable (Surround reference)."""
    valid = valid_entries(entries)
    slot = SURROUND_GROUP_SLOT["front"]
    return bool(front_roles) and all(entry_key(role, slot) in valid for role in front_roles)


def derive_slot_values(entries: Mapping[str, dict]) -> dict[str, dict[str, float]]:
    """Per role the cut-only trim of each slot, from the stored raw levels.

    The target of a family is its quietest valid raw level, so no trim is
    positive. A spread beyond ``MAX_PLAUSIBLE_SPREAD_DB`` is refused. The result
    has the shape of the ``set_level_calibration`` mutation: ``{role: {slot: dB}}``.
    """
    valid = valid_entries(entries)
    families: dict[str, list[dict]] = {}
    for entry in valid.values():
        families.setdefault(entry["family"], []).append(entry)
    values: dict[str, dict[str, float]] = {}
    for members in families.values():
        target = min(entry["raw_db"] for entry in members)
        for entry in members:
            trim = target - entry["raw_db"]
            if trim < -MAX_PLAUSIBLE_SPREAD_DB:
                raise ValueError(
                    f"Level difference of {-trim:.1f} dB at {entry['role']} is implausible; "
                    "check microphone position, input and wiring")
            trim = round(round(trim / TRIM_RESOLUTION_DB) * TRIM_RESOLUTION_DB, 3)
            values.setdefault(entry["role"], {})[entry["slot"]] = min(0.0, trim) + 0.0
    return values


def zero_slot_values(mode: str, roles: Sequence[str], scope: dict) -> dict[str, dict[str, float]]:
    """Every slot Level Matching owns in the scope's family at 0 dB.

    Has the shape of the ``set_level_calibration`` mutation. A Stereo scope resets
    its own slot; Surround is one family, so all speaker groups go back together.
    Subwoofer slots are never touched (Auto Sub Optimize owns them).
    """
    if mode == "surround":
        return {role: {SURROUND_GROUP_SLOT[surround_group(role)]: 0.0}
                for role in roles if role not in SUB_ROLES}
    return {role: {scope["slot"]: 0.0} for unit in scope["units"] for role in unit["roles"]}


def drop_family(entries: Mapping[str, dict], family: str) -> dict[str, dict]:
    """The stored raw levels without those of one family."""
    return {key: entry for key, entry in entries.items() if entry["family"] != family}


class LevelMatchStore:
    """Atomic JSON file with the stored raw levels per output mode."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema": SCHEMA, "version": VERSION, "modes": {}}
        except (ValueError, RecursionError) as exc:
            raise ValueError("Invalid level match JSON") from exc
        if (not isinstance(payload, dict) or payload.get("schema") != SCHEMA
                or payload.get("version") != VERSION or not isinstance(payload.get("modes"), dict)):
            raise ValueError("Unsupported level match document")
        return payload

    def entries(self, mode: str) -> dict[str, dict]:
        return copy.deepcopy(self.load()["modes"].get(mode, {}))

    def save_entries(self, mode: str, entries: Mapping[str, dict]) -> None:
        document = self.load()
        document["modes"][mode] = copy.deepcopy(dict(entries))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n")
