# SPDX-License-Identifier: AGPL-3.0-only
"""Per-output SPL calibration of the Loudness work point.

A calibration is measured through one output (amplifier, speakers, room), so it
belongs to that output and to no other. The records live in
``loudness.params.calibrationProfiles`` keyed by the output's device identity
(``device_key``); the output mode is not part of the identity. ``calibration``
is the derived view of the record of the output that is rendering: it is what
the work point and the display read, and an output without a record has none.
A calibration saved before outputs had an identity is kept as
``legacyCalibration`` and never used.

Pure functions over extras dictionaries: no files, no hardware.
"""

from __future__ import annotations

import copy
from typing import Any

from audio.output_routing import device_key


def profile_id(value: Any) -> str:
    """Device identity of an output key ("" when there is none)."""
    return device_key(value) if isinstance(value, str) and value.strip() else ""


def normalize_record(value: Any) -> dict[str, Any]:
    """One calibration record with its derived ``requiredAdjustmentDb`` and ``calibrated`` fields."""
    record = dict(value) if isinstance(value, dict) else {}
    adjustment = record.get("requiredAdjustmentDb")
    if isinstance(adjustment, (int, float)):
        record["requiredAdjustmentDb"] = float(adjustment)
        record["calibrated"] = abs(float(adjustment)) <= 1.0
    return record


def _newest(candidates: list[tuple[int, dict]]) -> dict:
    """The record with the latest ``createdAt``; among equals the one stored last."""
    return max(candidates, key=lambda item: (str(item[1].get("createdAt") or ""), item[0]))[1]


def migrate(params: dict) -> dict[str, dict]:
    """Profile map of ``params`` keyed by device identity.

    Keys that differ only by the USB profile suffix share one identity (the newest
    record wins). A ``calibration`` that names an output the map does not hold yet
    is added to it. Nothing is written to ``params``.
    """
    raw = params.get("calibrationProfiles") if isinstance(params.get("calibrationProfiles"), dict) else {}
    grouped: dict[str, list[tuple[int, dict]]] = {}
    for order, (key, record) in enumerate(raw.items()):
        identity = profile_id(str(key))
        if identity and isinstance(record, dict):
            grouped.setdefault(identity, []).append((order, normalize_record(record)))
    profiles = {identity: {**_newest(items), "outputProfileId": identity} for identity, items in grouped.items()}
    calibration = normalize_record(params.get("calibration"))
    identity = profile_id(calibration.get("outputProfileId"))
    if identity and identity not in profiles:
        profiles[identity] = {**calibration, "outputProfileId": identity}
    return profiles


def effective(params: dict, device_id: str | None) -> dict[str, Any]:
    """The calibration record of one output, ``{}`` when it has none (never another output's)."""
    identity = profile_id(device_id)
    return dict(migrate(params).get(identity) or {}) if identity else {}


def bind_calibration(extras: dict, device_id: str | None) -> dict:
    """A copy of ``extras`` whose ``calibration`` is the record of ``device_id``.

    The profile map is brought to device identities and a calibration without an
    output identity moves to ``legacyCalibration``, where it is kept and not used.
    """
    result = copy.deepcopy(extras)
    loudness = result.get("loudness") if isinstance(result, dict) else None
    params = loudness.get("params") if isinstance(loudness, dict) else None
    if not isinstance(params, dict):
        return result
    calibration = normalize_record(params.get("calibration"))
    if calibration and not profile_id(calibration.get("outputProfileId")) and not params.get("legacyCalibration"):
        params["legacyCalibration"] = calibration
    profiles = migrate(params)
    identity = profile_id(device_id)
    params["calibrationProfiles"] = profiles
    params["calibration"] = dict(profiles.get(identity) or {}) if identity else {}
    return result
