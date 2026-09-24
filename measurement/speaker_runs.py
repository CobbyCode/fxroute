# SPDX-License-Identifier: AGPL-3.0-only
"""Saveable Speaker Align runs: time-domain Before/After without re-measuring.

A run binds the two takes an align job already measured:

* Before: band-isolated way arrivals from the shared planning take
  (``SpeakerAlignment.planning`` / ``propose`` input).
* After: band-isolated way arrivals from the shared verification take
  (``SpeakerAlignment.confirmation`` / ``verify_confirmation`` input).

No new sweep is involved: :func:`time_domain_view` derives a shared ms
axis view purely from the stored Before/After arrival documents, so the
relative offset before and after the alignment is visible on one time
base. :func:`build_speaker_align_run` preserves the full existing
measurement dataset (ways, delay/gain corrections, QC/isolation,
metadata); per-way frequency points are passed through when the job
captured them and are otherwise omitted.
"""

from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

SPEAKER_ALIGN_RUN_SCHEMA = "speaker-align-run-v1"
SPEAKER_ALIGN_RUN_KIND = "speaker-align-run-v1"


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Speaker Align run {label} must be finite")
    return float(value)


def _role_map(value: object, label: str) -> dict[str, float]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"Speaker Align run {label} must be a non-empty object")
    out: dict[str, float] = {}
    for role, arrival in value.items():
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"Speaker Align run {label} carries no way name")
        out[role] = _finite(arrival, f"{label} arrival for {role}")
    return out


def _optional_role_map(value: object, label: str) -> dict[str, float | None]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Speaker Align run {label} must be an object")
    out: dict[str, float | None] = {}
    for role, margin in value.items():
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"Speaker Align run {label} carries no way name")
        if margin is None:
            out[role] = None
        else:
            out[role] = _finite(margin, f"{label} margin for {role}")
    return out


def _frequency_points(value: object, label: str) -> list[list[float]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"Speaker Align run {label} must be a non-empty point list")
    points: list[list[float]] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(f"Speaker Align run {label} point must be a pair")
        frequency = _finite(point[0], f"{label} frequency")
        level = _finite(point[1], f"{label} level")
        if frequency <= 0:
            raise ValueError(f"Speaker Align run {label} frequency must be positive")
        points.append([float(frequency), float(level)])
    points.sort(key=lambda pair: pair[0])
    return points


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_speaker_align_run(
    *,
    side: str,
    proposal: dict[str, Any],
    check: dict[str, Any],
    provenance: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    sample_rate_hz: int | None = None,
    job_id: str | None = None,
    dry_run: bool = False,
    committed_revision: Any = None,
    confirmed: bool | None = None,
    created_at: str | None = None,
    run_id: str | None = None,
    frequency: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a saveable run from a finished align job result.

    ``proposal`` is the ``SpeakerAlignment.propose`` output (Before from the
    planning take); ``check`` is the ``verify_confirmation`` output (After
    from the verification take). Ways, corrections, QC/isolation and
    metadata are preserved; ``frequency`` optionally carries per-way
    frequency points (trusted/review) which are passed through unchanged.
    """
    if side not in ("left", "right"):
        raise ValueError("Speaker Align run side must be left or right")
    if not isinstance(proposal, dict) or not isinstance(check, dict):
        raise ValueError("Speaker Align run needs proposal and check documents")
    before = _role_map(proposal.get("arrival_ms"), "before")
    after_raw = check.get("after_arrival_ms")
    if not isinstance(after_raw, dict):
        raise ValueError("Speaker Align run check carries no after arrivals")
    after = _role_map(after_raw, "after")
    if set(before) != set(after):
        raise ValueError("Speaker Align run Before and After name different ways")
    ways = sorted(before)
    added_delay = _role_map(proposal.get("added_delay_ms"), "delay correction")
    if set(added_delay) != set(before):
        raise ValueError("Speaker Align run delay corrections name different ways")
    added_gain_raw = proposal.get("added_gain_db") or {}
    way_levels_raw = proposal.get("way_levels_db") or {}
    added_gain = _role_map(added_gain_raw, "gain correction") if added_gain_raw else {role: 0.0 for role in ways}
    way_levels = _role_map(way_levels_raw, "way level") if way_levels_raw else {role: 0.0 for role in ways}
    if set(added_gain) != set(before) or set(way_levels) != set(before):
        raise ValueError("Speaker Align run gain/level evidence names different ways")
    planning_isolation = _optional_role_map(proposal.get("planning_isolation_db"), "planning isolation")
    after_isolation = _optional_role_map(check.get("way_isolation_db"), "verification isolation")
    if planning_isolation and set(planning_isolation) != set(before):
        raise ValueError("Speaker Align run planning isolation names different ways")
    if after_isolation and set(after_isolation) != set(before):
        raise ValueError("Speaker Align run verification isolation names different ways")
    reference_role = proposal.get("reference_role")
    if not isinstance(reference_role, str) or reference_role not in before:
        raise ValueError("Speaker Align run reference role is not one of the ways")
    start_revision = proposal.get("start_revision")
    if type(start_revision) is not int:
        raise ValueError("Speaker Align run carries no start revision")
    fingerprint = proposal.get("processing_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("Speaker Align run carries no processing fingerprint")
    max_residual = _finite(check.get("max_residual_ms"), "residual")
    before_spread = _finite(check.get("before_spread_ms"), "before spread")
    tolerance = _finite(check.get("tolerance_ms"), "tolerance")
    reasons = [str(item) for item in (check.get("reasons") or [])]
    warnings = [str(item) for item in (check.get("warnings") or [])]
    pairs = deepcopy(check.get("pairs") or [])
    gain_spread = check.get("gain_spread_db")
    before_gain_spread = check.get("before_gain_spread_db")
    gain_tolerance = check.get("gain_tolerance_db")
    after_levels_raw = check.get("after_way_levels_db") or {}
    after_levels = _role_map(after_levels_raw, "after level") if after_levels_raw else {}
    if after_levels and set(after_levels) != set(before):
        raise ValueError("Speaker Align run after levels name different ways")
    isolation_margin = check.get("isolation_margin_db")
    if isolation_margin is not None:
        isolation_margin = _finite(isolation_margin, "isolation margin")
    if sample_rate_hz is not None and (type(sample_rate_hz) is not int or sample_rate_hz <= 0):
        raise ValueError("Speaker Align run sample rate must be a positive integer")
    frequency_out: dict[str, Any] = {}
    if frequency is not None:
        if not isinstance(frequency, dict):
            raise ValueError("Speaker Align run frequency evidence must be an object")
        for role, panel in frequency.items():
            if role not in before:
                raise ValueError(f"Speaker Align run frequency evidence names unknown way {role}")
            if not isinstance(panel, dict):
                raise ValueError(f"Speaker Align run frequency evidence for {role} must be an object")
            entry: dict[str, Any] = {}
            if panel.get("trusted_points") is not None:
                entry["trusted_points"] = _frequency_points(panel["trusted_points"], f"{role} trusted")
            if panel.get("review_points") is not None:
                entry["review_points"] = _frequency_points(panel["review_points"], f"{role} review")
            if entry:
                frequency_out[role] = entry
    resolved_confirmed = bool(check.get("confirmed")) if confirmed is None else bool(confirmed)
    run = {
        "schema": SPEAKER_ALIGN_RUN_SCHEMA,
        "id": str(run_id or f"speaker-align-{uuid4().hex[:12]}"),
        "created_at": str(created_at or _utc_now()),
        "side": side,
        "job_id": str(job_id or ""),
        "dry_run": bool(dry_run),
        "confirmed": resolved_confirmed,
        "committed_revision": committed_revision,
        "sample_rate_hz": sample_rate_hz,
        "ways": list(ways),
        "reference_role": reference_role,
        "arrival_source": str(proposal.get("arrival_source") or "shared-planning-take"),
        "before": {
            "source": "planning-take",
            "arrival_ms": {role: before[role] for role in ways},
            "way_isolation_db": {role: planning_isolation.get(role) for role in ways} if planning_isolation else {},
            "way_levels_db": {role: way_levels[role] for role in ways},
        },
        "after": {
            "source": "verification-take",
            "arrival_ms": {role: after[role] for role in ways},
            "way_isolation_db": {role: after_isolation.get(role) for role in ways} if after_isolation else {},
            "way_levels_db": dict(after_levels) if after_levels else {},
        },
        "corrections": {
            "added_delay_ms": {role: added_delay[role] for role in ways},
            "added_gain_db": {role: added_gain[role] for role in ways},
        },
        "qc": {
            "confirmed": resolved_confirmed,
            "reasons": reasons,
            "warnings": warnings,
            "max_residual_ms": max_residual,
            "before_spread_ms": before_spread,
            "tolerance_ms": tolerance,
            "pairs": pairs,
            "gain_spread_db": None if gain_spread is None else _finite(gain_spread, "gain spread"),
            "before_gain_spread_db": None if before_gain_spread is None else _finite(
                before_gain_spread, "before gain spread"),
            "gain_tolerance_db": None if gain_tolerance is None else _finite(
                gain_tolerance, "gain tolerance"),
            "isolation_margin_db": isolation_margin,
        },
        "metadata": {
            "start_revision": start_revision,
            "processing_fingerprint": fingerprint,
            "provenance": deepcopy(provenance or {}),
            "params": deepcopy(params or {}),
        },
    }
    if frequency_out:
        run["frequency"] = frequency_out
    return validate_speaker_align_run(run)


def validate_speaker_align_run(run: dict[str, Any]) -> dict[str, Any]:
    """Validate a stored run document; return a detached canonical copy."""
    if not isinstance(run, dict):
        raise ValueError("Speaker Align run must be an object")
    if run.get("schema") != SPEAKER_ALIGN_RUN_SCHEMA:
        raise ValueError("Speaker Align run schema must be speaker-align-run-v1")
    side = run.get("side")
    if side not in ("left", "right"):
        raise ValueError("Speaker Align run side must be left or right")
    ways = run.get("ways")
    if not isinstance(ways, list) or len(ways) not in (2, 3, 4) or len(set(ways)) != len(ways):
        raise ValueError("Speaker Align run ways must be 2-4 distinct way names")
    for role in ways:
        if not isinstance(role, str) or not role.strip():
            raise ValueError("Speaker Align run carries no way name")
    before = run.get("before") or {}
    after = run.get("after") or {}
    if before.get("source") != "planning-take":
        raise ValueError("Speaker Align run Before must come from the planning take")
    if after.get("source") != "verification-take":
        raise ValueError("Speaker Align run After must come from the verification take")
    before_arrivals = _role_map(before.get("arrival_ms"), "before")
    after_arrivals = _role_map(after.get("arrival_ms"), "after")
    if set(before_arrivals) != set(ways) or set(after_arrivals) != set(ways):
        raise ValueError("Speaker Align run Before/After must name exactly the run ways")
    corrections = run.get("corrections") or {}
    delays = _role_map(corrections.get("added_delay_ms"), "delay correction")
    gains = _role_map(corrections.get("added_gain_db"), "gain correction")
    if set(delays) != set(ways) or set(gains) != set(ways):
        raise ValueError("Speaker Align run corrections must name exactly the run ways")
    qc = run.get("qc") or {}
    _finite(qc.get("max_residual_ms"), "residual")
    _finite(qc.get("before_spread_ms"), "before spread")
    _finite(qc.get("tolerance_ms"), "tolerance")
    if not isinstance(qc.get("reasons"), list) or not isinstance(qc.get("warnings"), list):
        raise ValueError("Speaker Align run QC reasons/warnings must be lists")
    metadata = run.get("metadata") or {}
    if type(metadata.get("start_revision")) is not int:
        raise ValueError("Speaker Align run carries no start revision")
    if not isinstance(metadata.get("processing_fingerprint"), str) or not metadata["processing_fingerprint"]:
        raise ValueError("Speaker Align run carries no processing fingerprint")
    frequency = run.get("frequency") or {}
    if frequency:
        if not isinstance(frequency, dict):
            raise ValueError("Speaker Align run frequency evidence must be an object")
        for role, panel in frequency.items():
            if role not in ways:
                raise ValueError(f"Speaker Align run frequency evidence names unknown way {role}")
            if not isinstance(panel, dict):
                raise ValueError(f"Speaker Align run frequency evidence for {role} must be an object")
            for key in ("trusted_points", "review_points"):
                if panel.get(key) is not None:
                    _frequency_points(panel[key], f"{role} {key}")
    return deepcopy(run)


def time_domain_view(run: dict[str, Any]) -> dict[str, Any]:
    """Derive the shared ms-axis Before/After view from a stored run.

    Pure derivation from the planning-take Before and the verification-take
    After: no sweep, no store, no I/O. Both lanes share one window so the
    relative way offset before and after the alignment is directly visible.
    """
    canonical = validate_speaker_align_run(run)
    ways = list(canonical["ways"])
    before = {role: float(canonical["before"]["arrival_ms"][role]) for role in ways}
    after = {role: float(canonical["after"]["arrival_ms"][role]) for role in ways}
    before_spread = max(before.values()) - min(before.values())
    after_spread = max(after.values()) - min(after.values())
    lowest = min(min(before.values()), min(after.values()))
    highest = max(max(before.values()), max(after.values()))
    span = highest - lowest
    margin = max(0.25, span * 0.15)
    window = [round(lowest - margin, 6), round(highest + margin, 6)]
    lanes = {
        "before": {
            "source": "planning-take",
            "arrival_ms": before,
            "spread_ms": round(before_spread, 6),
        },
        "after": {
            "source": "verification-take",
            "arrival_ms": after,
            "spread_ms": round(after_spread, 6),
        },
    }
    return {
        "schema": SPEAKER_ALIGN_RUN_SCHEMA,
        "side": canonical["side"],
        "ways": ways,
        "window_ms": window,
        "lanes": lanes,
        "reference_role": canonical["reference_role"],
        "qc": deepcopy(canonical["qc"]),
        "corrections": deepcopy(canonical["corrections"]),
    }


def run_to_measurement(run: dict[str, Any], *, name: str = "") -> dict[str, Any]:
    """Convert a run to a saved-measurement payload.

    Frequency points travel as regular traces when present (they are
    optional); the full run stays intact under ``speaker_align`` so a
    reopened measurement renders the same time-domain view plus the
    associated alignment data.
    """
    canonical = validate_speaker_align_run(run)
    label = str(name or "").strip() or (
        f"Speaker Align {canonical['side']} · "
        f"{'verified' if canonical['confirmed'] else 'not verified'}")
    traces: list[dict[str, Any]] = []
    colors = ["#4caf8a", "#7aa2f7", "#e0af68", "#bb9af7"]
    for index, role in enumerate(canonical["ways"]):
        panel = (canonical.get("frequency") or {}).get(role) or {}
        points = panel.get("trusted_points") or panel.get("review_points")
        if points:
            traces.append({
                "kind": "speaker-align-way-response",
                "label": f"{label} · {role}",
                "color": colors[index % len(colors)],
                "role": "trusted",
                "points": deepcopy(points),
            })
    payload: dict[str, Any] = {
        "id": canonical["id"],
        "name": label,
        "created_at": canonical["created_at"],
        "channel": canonical["side"],
        "measurement_kind": SPEAKER_ALIGN_RUN_KIND,
        "speaker_align": deepcopy(canonical),
        "analysis": {
            "method": "speaker-align-run-v1",
            "speaker_align": deepcopy(canonical),
        },
    }
    if traces:
        payload["traces"] = traces
    else:
        # Time-domain runs carry no frequency obligation; the persistence
        # layer accepts this kind without traces (see MeasurementPersistence).
        payload["traces"] = []
    return payload


def measurement_to_run(measurement: dict[str, Any]) -> dict[str, Any]:
    """Extract and validate the run stored in a saved measurement."""
    if not isinstance(measurement, dict):
        raise ValueError("Speaker Align measurement must be an object")
    run = measurement.get("speaker_align")
    if run is None and isinstance(measurement.get("analysis"), dict):
        run = measurement["analysis"].get("speaker_align")
    if run is None:
        raise ValueError("Saved measurement carries no Speaker Align run")
    canonical = validate_speaker_align_run(run)
    # Frequency traces saved alongside the run are evidence, not authority:
    # when the run itself carries no frequency panels, adopt matching saved
    # traces so a reopened run keeps the co-stored frequency view.
    if not canonical.get("frequency"):
        traces = measurement.get("traces") or []
        panels: dict[str, Any] = {}
        for trace in traces:
            if not isinstance(trace, dict):
                continue
            label = str(trace.get("label") or "")
            role = next((way for way in canonical["ways"] if way in label), None)
            points = trace.get("points")
            if role is None or not isinstance(points, list) or not points:
                continue
            try:
                panels[role] = {"trusted_points": _frequency_points(points, f"{role} trusted")}
            except ValueError:
                continue
        if panels:
            canonical = deepcopy(canonical)
            canonical["frequency"] = panels
    return canonical


__all__ = [
    "SPEAKER_ALIGN_RUN_KIND",
    "SPEAKER_ALIGN_RUN_SCHEMA",
    "build_speaker_align_run",
    "measurement_to_run",
    "run_to_measurement",
    "time_domain_view",
    "validate_speaker_align_run",
]
