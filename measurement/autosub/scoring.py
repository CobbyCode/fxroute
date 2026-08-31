# SPDX-License-Identifier: AGPL-3.0-only

"""Result ranking, winner selection, confidence, and the candidate ledger."""

from __future__ import annotations

import json
import logging
import math
import statistics
from typing import Any, Awaitable, Callable
from uuid import uuid4

from measurement.store import (
    _AUTO_SUB_ANCHOR_MAX_CORRECTION_DB,
    _auto_sub_band_mean_power_db,
    auto_sub_chain_anchor_db,
    score_sub_alignment_candidates,
)

from audio.samplerate.constants import OUTPUT_MODE_SUBWOOFER_21
from .candidates import (
    _AUTO_SUB_MIN_POLARITY_ACCEPT_GAIN,
    _auto_sub_22_name,
    _auto_sub_clamped_delay,
    _auto_sub_score_value,
    _auto_sub_step_ms,
)

logger = logging.getLogger(__name__)

_AUTO_SUB_DISPLAY_ANCHOR_MAX_SHIFT_DB: float = 1.5

_AUTO_SUB_MIN_ALIGNMENT_SCORE_GAIN: float = 0.01


def _auto_sub_rank_results(results: list[dict[str, Any]]) -> None:
    for rank, result in enumerate(results, start=1):
        result["rank"] = rank

def _auto_sub_has_points(result: dict[str, Any], key: str = "points") -> bool:
    points = result.get(key) or []
    return isinstance(points, list) and len(points) >= 3

def _auto_sub_delay_key(result: dict[str, Any]) -> float:
    return round(float(result.get("delay_ms", 0.0)), 2)

def _auto_sub_best_scan_result(results: list[dict[str, Any]], scan: str) -> dict[str, Any] | None:
    matches = [result for result in results if str(result.get("scan") or "coarse") == scan]
    if not matches:
        return None
    return max(matches, key=_auto_sub_score_value)

def _auto_sub_result_for_delay(results: list[dict[str, Any]], delay_ms: float) -> dict[str, Any] | None:
    delay_key = round(float(delay_ms), 2)
    for result in results:
        if round(float(result.get("delay_ms", 0.0)), 2) == delay_key:
            return result
    return None

def _auto_sub_shared_bass_offset(
    *point_sets: list,
    low_hz: float = 20.0,
    high_hz: float = 200.0,
) -> float:
    """Compute a single median dB offset from combined bass-region points.

    Used so that all traces within one AutoSub run share the same vertical
    reference, preserving Before/After and L/R relative level differences.
    """
    bass_dbs: list[float] = []
    for pts in point_sets:
        if not isinstance(pts, list):
            continue
        for p in pts:
            if not (isinstance(p, (list, tuple)) and len(p) >= 2):
                continue
            try:
                hz, db = float(p[0]), float(p[1])
                if low_hz <= hz <= high_hz:
                    bass_dbs.append(db)
            except (ValueError, TypeError):
                continue
    if not bass_dbs:
        return 0.0
    sorted_dbs = sorted(bass_dbs)
    mid = len(sorted_dbs) // 2
    return sorted_dbs[mid] if len(sorted_dbs) % 2 == 1 else (sorted_dbs[mid - 1] + sorted_dbs[mid]) / 2.0

def _auto_sub_display_offset_db(
    normalized_by_db: float, anchor_shift_db: float, shared_offset_db: float,
) -> float:
    """Return the calibrated-to-display offset for an adjusted trace."""
    return round(float(normalized_by_db) - float(anchor_shift_db) + float(shared_offset_db), 4)

def _validate_auto_sub_target_curve_snapshot(raw_snapshot: str) -> tuple[dict[str, Any] | None, str | None]:
    """Validate and detach the browser-selected Target Curve for one AutoSub job."""
    if not str(raw_snapshot or "").strip():
        return None, "Target Curve snapshot is missing"
    try:
        incoming = json.loads(raw_snapshot)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "Target Curve snapshot is not valid JSON"
    if not isinstance(incoming, dict):
        return None, "Target Curve snapshot must be an object"
    key = str(incoming.get("key") or "").strip()
    label = str(incoming.get("label") or "").strip()
    provenance = str(incoming.get("provenance") or "").strip()
    if not key or not label:
        return None, "Target Curve key and label are required"
    if provenance not in {"built_in", "uploaded"}:
        return None, "Target Curve provenance must be built_in or uploaded"
    raw_points = incoming.get("points")
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        return None, "Target Curve requires at least two points"
    points: list[list[float]] = []
    previous_frequency = 0.0
    for index, point in enumerate(raw_points):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None, f"Target Curve point {index + 1} must be [frequency_hz, db]"
        try:
            frequency_hz, db = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            return None, f"Target Curve point {index + 1} must contain numbers"
        if not math.isfinite(frequency_hz) or not math.isfinite(db):
            return None, f"Target Curve point {index + 1} must contain finite numbers"
        if frequency_hz <= 0:
            return None, f"Target Curve point {index + 1} frequency must be greater than zero"
        if frequency_hz <= previous_frequency:
            return None, "Target Curve frequencies must be strictly increasing"
        points.append([frequency_hz, db])
        previous_frequency = frequency_hz
    return {"key": key, "label": label, "provenance": provenance, "points": points}, None

def _auto_sub_select_polarity_shared_winner(
    scored_results: list[dict[str, Any]],
    *,
    incumbent_delay_ms: float = 0.0,
    min_score_gain: float = _AUTO_SUB_MIN_POLARITY_ACCEPT_GAIN,
) -> dict[str, Any]:
    """Decide a polarity flip from one shared normalization set.

    *scored_results* must come from scoring the incumbent (delay placeholder
    ``incumbent_delay_ms``) together with every inverted-polarity candidate
    (delay placeholders above it). Comparing within one set avoids the
    two-candidate min-max vote in which 0.1 dB differences flip binary
    metric wins.
    """
    incumbent_key = round(float(incumbent_delay_ms), 2)
    incumbent = _auto_sub_result_for_delay(scored_results, incumbent_key)
    incumbent_score = _auto_sub_score_value(incumbent) if incumbent else 0.0
    invert_rows = [
        result for result in scored_results
        if round(float(result.get("delay_ms", 0.0) or 0.0), 2) != incumbent_key
    ]
    best_invert = max(invert_rows, key=_auto_sub_score_value) if invert_rows else None
    best_invert_score = _auto_sub_score_value(best_invert) if best_invert else 0.0
    gain = best_invert_score - incumbent_score if best_invert else 0.0
    accepted = bool(best_invert) and incumbent is not None and gain >= min_score_gain
    return {
        "accepted": accepted,
        "score_gain": round(gain, 4),
        "min_score_gain": min_score_gain,
        "incumbent_score": round(incumbent_score, 4),
        "alternative_score": round(best_invert_score, 4),
        "alternative_delay_ms": best_invert.get("delay_ms") if best_invert else None,
        "candidate_count": len(scored_results),
        "reason": (
            "alternative_clearly_better_in_shared_set" if accepted
            else "incumbent_protected_unclear_advantage"
        ),
    }

def _auto_sub_display_anchor_reference_db(point_sets: list) -> float | None:
    """Median chain anchor across a run's sweeps for display level correction.

    Before/After traces are each corrected against this reference so a
    measurement-chain gain excursion on one sweep no longer fakes a level
    change in the graph. Relative differences between traces are preserved.
    """
    anchors = [auto_sub_chain_anchor_db(points) for points in point_sets]
    valid = sorted(anchor for anchor in anchors if anchor is not None)
    if len(valid) < 2:
        return None
    mid = len(valid) // 2
    return valid[mid] if len(valid) % 2 == 1 else (valid[mid - 1] + valid[mid]) / 2.0

def _auto_sub_applied_anchor_shift(points: list, reference_db: float | None) -> float:
    """The capped chain-anchor correction that would be applied to *points*."""
    if reference_db is None or not isinstance(points, list):
        return 0.0
    anchor = auto_sub_chain_anchor_db(points)
    if anchor is None:
        return 0.0
    shift = max(
        -_AUTO_SUB_DISPLAY_ANCHOR_MAX_SHIFT_DB,
        min(_AUTO_SUB_DISPLAY_ANCHOR_MAX_SHIFT_DB, reference_db - anchor),
    )
    return shift if abs(shift) >= 0.01 else 0.0

def _auto_sub_anchor_shifted_points(points: list, reference_db: float | None) -> list:
    """Shift display points by the capped chain-anchor correction."""
    if reference_db is None or not isinstance(points, list):
        return points
    anchor = auto_sub_chain_anchor_db(points)
    if anchor is None:
        return points
    shift = _auto_sub_applied_anchor_shift(points, reference_db)
    if shift == 0.0:
        return points
    return [[point[0], point[1] + shift] for point in points]

def _auto_sub_result_meta(
    job: dict[str, Any], mode: str, final_levels_db: dict[str, float],
    *, target_vertical_offset_db: float | None = None,
) -> dict[str, Any]:
    """Build the AutoSub result metadata embedded into saved measurements.

    The frontend renders this as the saved-measurement summary line (target
    label + final sub gains) instead of the generic timing line.  The target
    curve comes from the job's own target curve snapshot, never from the
    later-selected UI curve.

    *target_vertical_offset_db* records the run's scored anchor offset. The
    calibrated Main points let the frontend recompute that same robust anchor
    for whichever Target Curve is currently selected in the graph.
    """
    target_curve = job.get("target_curve") if isinstance(job.get("target_curve"), dict) else None
    meta: dict[str, Any] = {
        "target": json.loads(json.dumps(target_curve)) if target_curve else None,
    }
    if target_vertical_offset_db is not None:
        meta["target_vertical_offset_db"] = round(float(target_vertical_offset_db), 4)
    anchor = job.get("main_target_anchor") if isinstance(job.get("main_target_anchor"), dict) else {}
    anchor_sides = anchor.get("sides") if isinstance(anchor.get("sides"), dict) else {}
    main_reference_points: dict[str, list[list[float]]] = {}
    for side in ("left", "right"):
        side_data = anchor_sides.get(side) if isinstance(anchor_sides.get(side), dict) else {}
        aligned_points = side_data.get("aligned_points") if isinstance(side_data.get("aligned_points"), list) else []
        points: list[list[float]] = []
        for point in aligned_points:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            frequency_hz, main_db = float(point[0]), float(point[1])
            if math.isfinite(frequency_hz) and frequency_hz > 0 and math.isfinite(main_db):
                points.append([round(frequency_hz, 3), round(main_db, 3)])
        if points:
            main_reference_points[side] = points
    if all(side in main_reference_points for side in ("left", "right")):
        meta["main_reference_points"] = main_reference_points
    if mode == OUTPUT_MODE_SUBWOOFER_21:
        if "sub" in final_levels_db:
            meta["final_gains_db"] = {"sub": round(float(final_levels_db["sub"]), 2)}
    else:
        gains = {key: round(float(final_levels_db[key]), 2) for key in ("sub1", "sub2") if key in final_levels_db}
        if gains:
            meta["final_gains_db"] = gains
    return meta

def _auto_sub_measurement_from_sweep(
    sweep_result: dict[str, Any],
    label: str,
    name: str,
    offset_db: float | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert an AutoSub sweep result into a frontend-compatible measurement dict.

    When *offset_db* is provided it is used as the shared vertical reference
    for all traces.  When omitted, the shared offset is computed from the
    combined bass-region (20‑200 Hz) dB values of the L + R points inside
    *sweep_result*, which is the correct default for a single-sweep call
    (2.1 / 2.2 Mono).  Callers that need a cross-sweep shared offset
    (2.2 Stereo) can pre-compute it and pass it explicitly.

    When *meta* is provided it is embedded as ``autosub_meta`` so the saved
    measurement can display the run's target curve and final sub gains.
    """
    traces: list[dict[str, Any]] = []
    base_id = uuid4().hex[:12]

    left_points = sweep_result.get("points_left") or []
    right_points = sweep_result.get("points_right") or []

    if offset_db is None:
        offset_db = _auto_sub_shared_bass_offset(left_points, right_points)

    if isinstance(left_points, list) and len(left_points) >= 3:
        points = [[float(p[0]), float(p[1]) - offset_db] for p in left_points]
        traces.append({
            "kind": "measured",
            "label": f"{label} L",
            "role": "left",
            "points": points,
        })

    if isinstance(right_points, list) and len(right_points) >= 3:
        points = [[float(p[0]), float(p[1]) - offset_db] for p in right_points]
        traces.append({
            "kind": "measured",
            "label": f"{label} R",
            "role": "right",
            "points": points,
        })

    result = {
        "id": f"autosub-{base_id}",
        "name": name,
        "traces": traces,
    }
    if meta:
        result["autosub_meta"] = meta
    # Exact display-coordinate correction already applied to these traces:
    # displayed = calibrated - display_offset_db, where
    # display_offset_db = normalized_by_db - anchor_shift + shared_offset_db.
    # Scoring places the target at (target + tvo) in calibrated coordinates,
    # so the frontend draws it at target + tvo - display_offset_db.
    left_nb = sweep_result.get("normalized_by_db_left")
    right_nb = sweep_result.get("normalized_by_db_right")
    left_shift = sweep_result.get("display_anchor_shift_db_left") or 0.0
    right_shift = sweep_result.get("display_anchor_shift_db_right") or 0.0
    if isinstance(left_nb, (int, float)) and len(left_points) >= 3:
        traces[0]["display_offset_db"] = _auto_sub_display_offset_db(left_nb, left_shift, offset_db)
    if isinstance(right_nb, (int, float)) and len(right_points) >= 3:
        right_trace_index = 1 if traces else 0
        traces[right_trace_index]["display_offset_db"] = _auto_sub_display_offset_db(
            right_nb, right_shift, offset_db,
        )
    return result

def _auto_sub_select_accepted_winner(
    *,
    coarse_winner: dict[str, Any],
    fine_winner: dict[str, Any] | None,
    incumbent_winner: dict[str, Any] | None,
    score_epsilon: float = _AUTO_SUB_MIN_ALIGNMENT_SCORE_GAIN,
) -> dict[str, Any]:
    protected_winner = coarse_winner
    if incumbent_winner is not None and (
        _auto_sub_score_value(incumbent_winner) + score_epsilon >= _auto_sub_score_value(coarse_winner)
    ):
        protected_winner = incumbent_winner

    accepted_winner = protected_winner
    fine_accepted = False
    reject_reason = None

    if fine_winner is None:
        reject_reason = "fine_not_better"
    elif _auto_sub_score_value(fine_winner) <= _auto_sub_score_value(protected_winner) + score_epsilon:
        reject_reason = "incumbent_better" if protected_winner is incumbent_winner else "fine_not_better"
    else:
        fine_xo_loss = max(
            0.0,
            float(coarse_winner.get("xo_score", 0.0) or 0.0) - float(fine_winner.get("xo_score", 0.0) or 0.0),
        )
        fine_timing_loss = max(
            0.0,
            float(coarse_winner.get("timing_band_score", 0.0) or 0.0)
            - float(fine_winner.get("timing_band_score", 0.0) or 0.0),
        )
        low_guard_gain_db = max(
            0.0,
            float(coarse_winner.get("low_guard_loss_db", 0.0) or 0.0)
            - float(fine_winner.get("low_guard_loss_db", 0.0) or 0.0),
        )
        if low_guard_gain_db <= 1.0 and (fine_xo_loss >= 0.05 or fine_timing_loss >= 0.05):
            reject_reason = "xo_loss_vs_coarse"
        else:
            accepted_winner = fine_winner
            fine_accepted = True

    return {
        "accepted_winner": accepted_winner,
        "fine_accepted": fine_accepted,
        "reject_reason": reject_reason,
        "protected_winner": protected_winner,
        "incumbent_winner": incumbent_winner,
        "incumbent_score": round(_auto_sub_score_value(incumbent_winner), 4) if incumbent_winner else None,
    }

def _auto_sub_scoring_confidence(results: list[dict[str, Any]]) -> str:
    if len(results) < 2:
        return "uncertain"
    winner = results[0]
    runner_up = results[1]
    winner_score = float(winner.get("score", 0.0) or 0.0)
    if winner_score <= 0:
        return "uncertain"
    margin = (winner_score - float(runner_up.get("score", 0.0) or 0.0)) / winner_score
    if margin > 0.15:
        return "clear"
    if margin > 0.05:
        return "close"
    return "uncertain"


# ---------------------------------------------------------------------------
# Candidate plausibility gate
# ---------------------------------------------------------------------------

# A delay change only redistributes band-integrated main+sub energy: with main
# M and sub S the wide-band sum |M + S*e^{jphi}|^2 averages to |M|^2 + |S|^2,
# independent of the delay phase phi. Neighbouring delays therefore cannot
# move the calibrated energy density over the scorer's evaluated region
# (0.35*fc..2*fc) by more than measurement noise. A large deviation is a
# capture/analysis artifact (for example a collapsed bass band despite a
# normal 200-600 Hz chain anchor) and must not enter the scorer's min-max
# normalization set, where one such sweep defined both ends of every metric
# and flipped a real winner decision.
_AUTO_SUB_PLAUSIBILITY_MAX_DEVIATION_DB: float = 6.0
_AUTO_SUB_PLAUSIBILITY_MIN_BAND_POINTS: int = 10
_AUTO_SUB_PLAUSIBILITY_MIN_ASSESSABLE: int = 3
_AUTO_SUB_PLAUSIBILITY_EXCLUSION_REASON: str = "implausible_bass_energy"


def _auto_sub_gate_row_alignment_key(row: dict[str, Any]) -> tuple[float, ...]:
    """Alignment coordinates of one candidate row (pair-aware for 2.2 mono)."""
    sub1 = row.get("sub1_alignment_ms")
    sub2 = row.get("sub2_alignment_ms")
    if isinstance(sub1, (int, float)) and isinstance(sub2, (int, float)):
        return (round(float(sub1), 2), round(float(sub2), 2))
    return (round(float(row.get("delay_ms", 0.0) or 0.0), 2),)


def _auto_sub_gate_row_distance(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Delay distance between two rows; the larger axis for pair rows."""
    if len(a) == 2 and len(b) == 2:
        return max(abs(a[0] - b[0]), abs(a[1] - b[1]))
    return abs(a[0] - b[0])


def _auto_sub_gate_candidate_rows(
    rows: list[dict[str, Any]],
    crossover_hz: int,
    *,
    context: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Exclude physically implausible candidate sweeps from a scoring set.

    Compares each candidate's calibrated band-energy density (power domain,
    never summed dB) over the scorer's evaluated region against the robust
    set median, with the same capped 200-600 Hz chain-anchor correction the
    scorer itself uses. Both collapse and inflation beyond
    ``_AUTO_SUB_PLAUSIBILITY_MAX_DEVIATION_DB`` are implausible.

    Conservative by design: rows without a usable calibration coordinate or
    band support are never excluded, the gate needs at least three
    assessable candidates, a strict majority of normal candidates (a broadly
    broken chain state excludes nothing), and for every excluded row at least
    one normal neighbour within one scan step — the defect must be specific
    to this candidate, not shared by its neighbours. Scoring formulas,
    weights and all remaining candidates are untouched.

    Works on single-side rows (``points``/``normalized_by_db``) and on dual
    L/R rows (``points_left``/``points_right``); a dual row is excluded when
    either of its measured sides is implausible. Excluded rows are marked in
    place with ``exclusion_reason``/``plausibility`` so the candidate ledger
    reports them; the returned list drops them from the scoring input.

    Returns ``(kept_rows, exclusions)``.
    """
    fc = float(crossover_hz)
    band_low_hz = fc * 0.35
    band_high_hz = fc * 2.0
    entries: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row.get("points_left"), list) or isinstance(row.get("points_right"), list):
            side_keys = (
                ("left", "points_left", "normalized_by_db_left"),
                ("right", "points_right", "normalized_by_db_right"),
            )
        else:
            side_keys = (("main", "points", "normalized_by_db"),)
        for side, points_key, normalized_key in side_keys:
            points = row.get(points_key) or []
            normalized_by = row.get(normalized_key)
            if not isinstance(normalized_by, (int, float)) or not math.isfinite(float(normalized_by)):
                continue
            calibrated = [
                [point[0], point[1] + float(normalized_by)]
                for point in points
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
            power_db = _auto_sub_band_mean_power_db(
                calibrated, band_low_hz, band_high_hz,
                min_points=_AUTO_SUB_PLAUSIBILITY_MIN_BAND_POINTS,
            )
            if power_db is None:
                continue
            entries.append({
                "row": row,
                "side": side,
                "key": _auto_sub_gate_row_alignment_key(row),
                "power_db": power_db,
                "anchor_db": auto_sub_chain_anchor_db(points),
            })
    if len(entries) < _AUTO_SUB_PLAUSIBILITY_MIN_ASSESSABLE:
        return list(rows), []

    by_side: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_side.setdefault(entry["side"], []).append(entry)
    for group in by_side.values():
        anchors = sorted(entry["anchor_db"] for entry in group if entry["anchor_db"] is not None)
        anchor_reference = statistics.median(anchors) if len(anchors) >= 2 else None
        for entry in group:
            correction = 0.0
            if anchor_reference is not None and entry["anchor_db"] is not None:
                correction = max(
                    -_AUTO_SUB_ANCHOR_MAX_CORRECTION_DB,
                    min(_AUTO_SUB_ANCHOR_MAX_CORRECTION_DB, anchor_reference - entry["anchor_db"]),
                )
            entry["corrected_power_db"] = entry["power_db"] + correction
        reference_power_db = statistics.median(entry["corrected_power_db"] for entry in group)
        for entry in group:
            entry["reference_power_db"] = reference_power_db
            deviation = entry["corrected_power_db"] - reference_power_db
            entry["deviation_db"] = round(deviation, 2)
            entry["verdict"] = (
                "low" if deviation < -_AUTO_SUB_PLAUSIBILITY_MAX_DEVIATION_DB
                else "high" if deviation > _AUTO_SUB_PLAUSIBILITY_MAX_DEVIATION_DB
                else "normal"
            )

    normals = [entry for entry in entries if entry["verdict"] == "normal"]
    if len(normals) * 2 <= len(entries):
        # No robust normal majority: the shared chain state itself is
        # suspect, so nothing is excluded (conservative fallback).
        return list(rows), []

    step_ms = _auto_sub_step_ms(crossover_hz)
    exclusions: list[dict[str, Any]] = []
    excluded_rows: set[int] = set()
    for entry in entries:
        if entry["verdict"] == "normal":
            continue
        neighbour_ok = any(
            other is not entry
            and other["verdict"] == "normal"
            and other["side"] == entry["side"]
            and _auto_sub_gate_row_distance(other["key"], entry["key"]) <= step_ms + 1e-9
            for other in entries
        )
        if not neighbour_ok:
            # The deviation is not specific to this candidate; leave it in.
            entry["verdict"] = "unconfirmed"
            continue
        entry["verdict"] = "excluded"
        excluded_rows.add(id(entry["row"]))
        details = {
            "side": entry["side"],
            "band_hz": [round(band_low_hz, 1), round(band_high_hz, 1)],
            "mean_power_db": round(entry["power_db"], 2),
            "reference_power_db": round(entry["reference_power_db"], 2),
            "deviation_db": entry["deviation_db"],
            "bound_db": _AUTO_SUB_PLAUSIBILITY_MAX_DEVIATION_DB,
            "chain_anchor_db": (
                round(entry["anchor_db"], 2) if entry["anchor_db"] is not None else None
            ),
            "alignment": list(entry["key"]),
        }
        row = entry["row"]
        row["exclusion_reason"] = _AUTO_SUB_PLAUSIBILITY_EXCLUSION_REASON
        row["plausibility"] = details
        exclusions.append({"delay_ms": row.get("delay_ms"), **details})

    if not excluded_rows:
        return list(rows), []
    kept = [row for row in rows if id(row) not in excluded_rows]
    logger.info(
        "AUTOSUB_PLAUSIBILITY %s excluded=%d kept=%d details=%s",
        context or "unspecified", len(exclusions), len(kept),
        json.dumps(exclusions, sort_keys=True),
    )
    return kept, exclusions


# ---------------------------------------------------------------------------
# Uncertain near-tie re-measurement
# ---------------------------------------------------------------------------

def _auto_sub_needs_tiebreak(scoring: dict[str, Any]) -> bool:
    """True when the scorer itself reports an uncertain top-two near-tie."""
    return bool(
        scoring.get("confidence") == "uncertain"
        and scoring.get("runner_up") is not None
        and scoring.get("results")
    )


async def _auto_sub_remeasure_tiebreak(
    *,
    scoring: dict[str, Any],
    rows: list[dict[str, Any]],
    measure: Callable[[float, int], Awaitable[dict[str, Any]]],
    crossover_hz: int,
    low_guard_reference_delay_ms: float | None = None,
) -> dict[str, Any] | None:
    """Confirm an uncertain near-tie with one fresh sweep per top candidate.

    The top two candidates are re-measured once with the unchanged scan
    configuration. Successfully re-measured points replace the original
    points in place — the same row dicts, so downstream gain and incumbent
    lookups see the confirmed data — and the full candidate set is re-scored
    with the unchanged scorer, so the decision is made from confirmed
    measurements. Returned sweep failures keep the original decision and are
    reported in the diagnostics; raised sweep errors (peak safety) propagate.
    """
    if not _auto_sub_needs_tiebreak(scoring):
        return None
    top_results = scoring["results"][:2]
    top_delays = [_auto_sub_delay_key(result) for result in top_results]
    measured_results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, delay in enumerate(top_delays):
        result = await measure(float(delay), index)
        if result.get("status") == "completed" and _auto_sub_has_points(result):
            measured_results.append(result)
        else:
            failures.append({
                "delay_ms": delay,
                "status": result.get("status"),
                "error": result.get("error"),
            })
    original_winner = top_results[0].get("delay_ms")
    diagnostics: dict[str, Any] = {
        "triggered": True,
        "reason": "confidence_uncertain",
        "confidence_before": scoring.get("confidence"),
        "top_before": [
            {"delay_ms": result.get("delay_ms"), "score": result.get("score")}
            for result in top_results
        ],
        "remeasured": [],
        "measure_failures": failures,
        "applied": False,
    }
    if not measured_results:
        diagnostics["reason"] = "confidence_uncertain_remeasure_failed"
        return {
            "applied": False, "scoring": None, "rows": rows,
            "measured_results": [], "diagnostics": diagnostics,
        }

    rows_by_delay = {_auto_sub_delay_key(row): row for row in rows}
    for result in measured_results:
        row = rows_by_delay.get(_auto_sub_delay_key(result))
        if row is None:
            continue
        update = {
            "points": result.get("points") or [],
            "normalized_by_db": result.get("normalized_by_db"),
            "sweep_id": result.get("sweep_id", row.get("sweep_id", "")),
            "tiebreak_remeasured": True,
        }
        if result.get("calibrated_points"):
            update["calibrated_points"] = result["calibrated_points"]
        row.update(update)
        diagnostics["remeasured"].append({
            "delay_ms": result.get("delay_ms"), "status": result.get("status"),
        })
    new_scoring = score_sub_alignment_candidates(
        rows,
        crossover_hz=crossover_hz,
        low_guard_reference_delay_ms=low_guard_reference_delay_ms,
    )
    diagnostics.update({
        "applied": True,
        "confidence_after": new_scoring.get("confidence"),
        "top_after": [
            {"delay_ms": result.get("delay_ms"), "score": result.get("score")}
            for result in new_scoring["results"][:2]
        ],
        "winner_before": original_winner,
        "winner_after": new_scoring["results"][0].get("delay_ms"),
        "changed": new_scoring["results"][0].get("delay_ms") != original_winner,
    })
    return {
        "applied": True,
        "scoring": new_scoring,
        "rows": rows,
        "measured_results": measured_results,
        "diagnostics": diagnostics,
    }

def _auto_sub_score_single_channel_fallback(
    candidates: list[dict[str, Any]],
    *,
    crossover_hz: int,
    channel_name: str,
    low_guard_reference_delay_ms: float | None = None,
) -> dict[str, Any]:
    scoring = score_sub_alignment_candidates(
        candidates,
        crossover_hz=crossover_hz,
        low_guard_reference_delay_ms=low_guard_reference_delay_ms,
    )
    scan_by_delay = {_auto_sub_delay_key(candidate): candidate.get("scan", "coarse") for candidate in candidates}
    for result in scoring.get("results", []):
        result["scan"] = scan_by_delay.get(_auto_sub_delay_key(result), result.get("scan", "coarse"))
        result["score_source"] = channel_name
        score = round(float(result.get("score", 0.0) or 0.0), 4)
        score_pct = round(score * 100.0, 1)
        result.setdefault("score", score)
        result.setdefault("score_pct", score_pct)
        if channel_name == "left":
            result["score_L"] = score
            result["score_L_pct"] = score_pct
            result["score_R"] = None
            result["score_R_pct"] = None
        else:
            result["score_L"] = None
            result["score_L_pct"] = None
            result["score_R"] = score
            result["score_R_pct"] = score_pct
    _auto_sub_rank_results(scoring["results"])
    scoring["winner"] = scoring["results"][0]
    scoring["runner_up"] = scoring["results"][1] if len(scoring["results"]) >= 2 else None
    scoring["confidence"] = _auto_sub_scoring_confidence(scoring["results"])
    scoring["score_mode"] = f"{channel_name}_fallback"
    scoring["scored_candidates"] = candidates
    return scoring

def _score_auto_sub_combined_candidates(
    candidates: list[dict[str, Any]],
    *,
    crossover_hz: int,
    low_guard_reference_delay_ms: float | None = None,
) -> dict[str, Any]:
    """Score AutoSub candidates with L/R data when available, fallback to one side."""
    both_valid = [
        result for result in candidates
        if _auto_sub_has_points(result, "points_left") and _auto_sub_has_points(result, "points_right")
    ]
    if len(both_valid) >= 2:
        valid_left = []
        valid_right = []
        scan_by_delay = {}
        for result in both_valid:
            delay_key = _auto_sub_delay_key(result)
            scan_by_delay[delay_key] = result.get("scan", "coarse")
            left_result = dict(result)
            left_result["points"] = result["points_left"]
            valid_left.append(left_result)
            right_result = dict(result)
            right_result["points"] = result["points_right"]
            valid_right.append(right_result)

        left_scoring = score_sub_alignment_candidates(
            valid_left,
            crossover_hz=crossover_hz,
            low_guard_reference_delay_ms=low_guard_reference_delay_ms,
        )
        right_scoring = score_sub_alignment_candidates(
            valid_right,
            crossover_hz=crossover_hz,
            low_guard_reference_delay_ms=low_guard_reference_delay_ms,
        )
        left_by_delay = {_auto_sub_delay_key(result): result for result in left_scoring["results"]}
        right_by_delay = {_auto_sub_delay_key(result): result for result in right_scoring["results"]}

        combined_results = []
        for result in both_valid:
            delay_key = _auto_sub_delay_key(result)
            left_result = left_by_delay.get(delay_key)
            right_result = right_by_delay.get(delay_key)
            if not left_result or not right_result:
                continue
            score_left = float(left_result.get("score", 0.0) or 0.0)
            score_right = float(right_result.get("score", 0.0) or 0.0)
            combined_score = 0.6 * min(score_left, score_right) + 0.4 * ((score_left + score_right) / 2.0)
            low_guard_loss = max(
                float(left_result.get("low_guard_loss_db", 0.0) or 0.0),
                float(right_result.get("low_guard_loss_db", 0.0) or 0.0),
            )
            low_guard_penalty = 0.6 * max(
                float(left_result.get("low_guard_penalty", 0.0) or 0.0),
                float(right_result.get("low_guard_penalty", 0.0) or 0.0),
            ) + 0.4 * (
                (
                    float(left_result.get("low_guard_penalty", 0.0) or 0.0)
                    + float(right_result.get("low_guard_penalty", 0.0) or 0.0)
                ) / 2.0
            )
            combined_results.append({
                "delay_ms": result["delay_ms"],
                "name": result.get("name", str(result["delay_ms"])),
                "score": round(combined_score, 4),
                "score_pct": round(combined_score * 100.0, 1),
                "xo_score": round((float(left_result.get("xo_score", 0.0) or 0.0) + float(right_result.get("xo_score", 0.0) or 0.0)) / 2.0, 4),
                "timing_band_score": round((float(left_result.get("timing_band_score", 0.0) or 0.0) + float(right_result.get("timing_band_score", 0.0) or 0.0)) / 2.0, 4),
                "low_guard_loss_db": round(low_guard_loss, 2),
                "low_guard_penalty": round(low_guard_penalty, 4),
                "final_score": round(combined_score, 4),
                "low_guard_loss_L_db": left_result.get("low_guard_loss_db"),
                "low_guard_loss_R_db": right_result.get("low_guard_loss_db"),
                "low_guard_penalty_L": left_result.get("low_guard_penalty"),
                "low_guard_penalty_R": right_result.get("low_guard_penalty"),
                "score_L": round(score_left, 4),
                "score_L_pct": round(score_left * 100.0, 1),
                "score_R": round(score_right, 4),
                "score_R_pct": round(score_right * 100.0, 1),
                "scan": scan_by_delay.get(delay_key, "coarse"),
                "score_source": "lr_combined",
            })

        if not combined_results:
            raise ValueError("No matching L/R AutoSub scoring results")

        combined_results.sort(key=lambda r: r["score"], reverse=True)
        _auto_sub_rank_results(combined_results)
        return {
            "winner": combined_results[0],
            "runner_up": combined_results[1] if len(combined_results) >= 2 else None,
            "results": combined_results,
            "confidence": _auto_sub_scoring_confidence(combined_results),
            "crossover_hz": crossover_hz,
            "score_mode": "lr_combined",
            "scored_candidates": both_valid,
        }

    left_valid = []
    right_valid = []
    for result in candidates:
        if _auto_sub_has_points(result, "points_left"):
            left_result = dict(result)
            left_result["points"] = result["points_left"]
            left_valid.append(left_result)
        if _auto_sub_has_points(result, "points_right"):
            right_result = dict(result)
            right_result["points"] = result["points_right"]
            right_valid.append(right_result)

    if left_valid and len(left_valid) >= len(right_valid):
        return _auto_sub_score_single_channel_fallback(
            left_valid,
            crossover_hz=crossover_hz,
            channel_name="left",
            low_guard_reference_delay_ms=low_guard_reference_delay_ms,
        )
    if right_valid:
        return _auto_sub_score_single_channel_fallback(
            right_valid,
            crossover_hz=crossover_hz,
            channel_name="right",
            low_guard_reference_delay_ms=low_guard_reference_delay_ms,
        )
    raise ValueError("No valid AutoSub sweep results to score")

def _score_auto_sub_matrix_candidates(
    candidates: list[dict[str, Any]],
    *,
    crossover_hz: int,
    original_sub1_alignment_ms: float | None = None,
    original_sub2_alignment_ms: float | None = None,
) -> dict[str, Any]:
    """Score measured 2.2 matrix candidates by Sub1/Sub2 alignment pair."""
    indexed = [
        (idx, result) for idx, result in enumerate(candidates)
        if _auto_sub_has_points(result, "points_left") or _auto_sub_has_points(result, "points_right")
    ]
    if not indexed:
        raise ValueError("No valid AutoSub 2.2 matrix sweep results to score")

    def _low_guard_p20(points: list[list[float]]) -> float:
        low_guard_min_hz = float(crossover_hz) * 0.35
        low_guard_max_hz = float(crossover_hz) * 0.75
        band = [float(point[1]) for point in points if low_guard_min_hz <= float(point[0]) < low_guard_max_hz]
        if not band:
            return float("-inf")
        band.sort()
        p20_index = min(len(band) - 1, max(0, int(round((len(band) - 1) * 0.20))))
        return band[p20_index]

    def _incumbent_index(rows: list[tuple[int, dict[str, Any]]]) -> int | None:
        if original_sub1_alignment_ms is None or original_sub2_alignment_ms is None:
            return None
        for idx, result in rows:
            if _is_incumbent_pair(result):
                return idx
        return None

    def _is_incumbent_pair(result: dict[str, Any]) -> bool:
        if original_sub1_alignment_ms is None or original_sub2_alignment_ms is None:
            return False
        original_sub1 = _auto_sub_clamped_delay(float(original_sub1_alignment_ms))
        original_sub2 = _auto_sub_clamped_delay(float(original_sub2_alignment_ms))
        sub1_alignment = _auto_sub_clamped_delay(float(result.get("sub1_alignment_ms", 0.0) or 0.0))
        sub2_alignment = _auto_sub_clamped_delay(float(result.get("sub2_alignment_ms", 0.0) or 0.0))
        return abs(sub1_alignment - original_sub1) <= 0.05 and abs(sub2_alignment - original_sub2) <= 0.05

    def _reference_index(rows: list[tuple[int, dict[str, Any]]], points_key: str) -> tuple[int | None, str]:
        incumbent_idx = _incumbent_index(rows)
        if incumbent_idx is not None:
            return incumbent_idx, "incumbent"
        valid = [
            (idx, _low_guard_p20(result.get(points_key) or []))
            for idx, result in rows
            if _auto_sub_has_points(result, points_key)
        ]
        if not valid:
            return None, "matrix_best_low_guard"
        return max(valid, key=lambda item: item[1])[0], "matrix_best_low_guard"

    def _copy_for_score(
        result: dict[str, Any],
        idx: int,
        points_key: str,
        reference_idx: int | None,
        reference_label: str,
    ) -> dict[str, Any]:
        candidate = dict(result)
        candidate["delay_ms"] = float(idx)
        candidate["name"] = result.get("name") or _auto_sub_22_name(
            float(result.get("sub1_alignment_ms", 0.0) or 0.0),
            float(result.get("sub2_alignment_ms", 0.0) or 0.0),
        )
        candidate["points"] = result.get(points_key) or []
        if reference_idx is not None and idx == reference_idx:
            candidate["low_guard_reference"] = True
            candidate["low_guard_reference_label"] = reference_label
        return candidate

    def _combined_low_guard_reference(left_result: dict[str, Any], right_result: dict[str, Any]) -> str:
        left_ref = str(left_result.get("low_guard_reference") or "")
        right_ref = str(right_result.get("low_guard_reference") or "")
        if left_ref == right_ref:
            return left_ref
        if {left_ref, right_ref} <= {"incumbent", "matrix_best_low_guard"}:
            return "mixed"
        return f"L:{left_ref} / R:{right_ref}"

    def _finalize_matrix_scoring(results: list[dict[str, Any]], *, score_mode: str, scored_candidates: list[dict[str, Any]]) -> dict[str, Any]:
        _auto_sub_rank_results(results)
        incumbent_winner = next((result for result in results if bool(result.get("incumbent_pair"))), None)
        matrix_winner = next((result for result in results if not bool(result.get("incumbent_pair"))), None)
        if matrix_winner is None:
            matrix_winner = results[0]

        accepted_winner = matrix_winner
        incumbent_accepted = False
        reject_reason = "matrix_better"
        if incumbent_winner is not None:
            incumbent_score = _auto_sub_score_value(incumbent_winner)
            matrix_score = _auto_sub_score_value(matrix_winner)
            if matrix_score <= incumbent_score + _AUTO_SUB_MIN_ALIGNMENT_SCORE_GAIN:
                accepted_winner = incumbent_winner
                incumbent_accepted = True
                reject_reason = (
                    "incumbent_better"
                    if matrix_score <= incumbent_score
                    else "incumbent_gain_below_minimum"
                )

        return {
            "winner": accepted_winner,
            "runner_up": results[1] if len(results) >= 2 else None,
            "results": results,
            "confidence": _auto_sub_scoring_confidence(results),
            "crossover_hz": crossover_hz,
            "score_mode": score_mode,
            "scored_candidates": scored_candidates,
            "matrix_winner": matrix_winner,
            "incumbent_winner": incumbent_winner,
            "incumbent_score": round(_auto_sub_score_value(incumbent_winner), 4) if incumbent_winner else None,
            "accepted_winner": accepted_winner,
            "incumbent_accepted": incumbent_accepted,
            "reject_reason": reject_reason,
        }

    both_valid = [
        (idx, result) for idx, result in indexed
        if _auto_sub_has_points(result, "points_left") and _auto_sub_has_points(result, "points_right")
    ]
    if len(both_valid) >= 2:
        left_reference_idx, left_reference_label = _reference_index(both_valid, "points_left")
        right_reference_idx, right_reference_label = _reference_index(both_valid, "points_right")
        left_scoring = score_sub_alignment_candidates(
            [_copy_for_score(result, idx, "points_left", left_reference_idx, left_reference_label) for idx, result in both_valid],
            crossover_hz=crossover_hz,
        )
        right_scoring = score_sub_alignment_candidates(
            [_copy_for_score(result, idx, "points_right", right_reference_idx, right_reference_label) for idx, result in both_valid],
            crossover_hz=crossover_hz,
        )
        left_by_idx = {int(round(float(result.get("delay_ms", 0.0)))): result for result in left_scoring["results"]}
        right_by_idx = {int(round(float(result.get("delay_ms", 0.0)))): result for result in right_scoring["results"]}
        combined_results = []
        for idx, result in both_valid:
            left_result = left_by_idx.get(idx)
            right_result = right_by_idx.get(idx)
            if not left_result or not right_result:
                continue
            score_left = float(left_result.get("score", 0.0) or 0.0)
            score_right = float(right_result.get("score", 0.0) or 0.0)
            combined_score = 0.6 * min(score_left, score_right) + 0.4 * ((score_left + score_right) / 2.0)
            low_guard_loss = max(
                float(left_result.get("low_guard_loss_db", 0.0) or 0.0),
                float(right_result.get("low_guard_loss_db", 0.0) or 0.0),
            )
            low_guard_penalty = 0.6 * max(
                float(left_result.get("low_guard_penalty", 0.0) or 0.0),
                float(right_result.get("low_guard_penalty", 0.0) or 0.0),
            ) + 0.4 * (
                (
                    float(left_result.get("low_guard_penalty", 0.0) or 0.0)
                    + float(right_result.get("low_guard_penalty", 0.0) or 0.0)
                ) / 2.0
            )
            sub1_alignment = _auto_sub_clamped_delay(float(result.get("sub1_alignment_ms", 0.0) or 0.0))
            sub2_alignment = _auto_sub_clamped_delay(float(result.get("sub2_alignment_ms", 0.0) or 0.0))
            combined_results.append({
                "delay_ms": sub1_alignment,
                "sub1_alignment_ms": sub1_alignment,
                "sub2_alignment_ms": sub2_alignment,
                "incumbent_pair": _is_incumbent_pair(result),
                "name": result.get("name") or _auto_sub_22_name(sub1_alignment, sub2_alignment),
                "score": round(combined_score, 4),
                "score_pct": round(combined_score * 100.0, 1),
                "xo_score": round((float(left_result.get("xo_score", 0.0) or 0.0) + float(right_result.get("xo_score", 0.0) or 0.0)) / 2.0, 4),
                "timing_band_score": round((float(left_result.get("timing_band_score", 0.0) or 0.0) + float(right_result.get("timing_band_score", 0.0) or 0.0)) / 2.0, 4),
                "low_guard_loss_db": round(low_guard_loss, 2),
                "low_guard_penalty": round(low_guard_penalty, 4),
                "final_score": round(combined_score, 4),
                "low_guard_loss_L_db": left_result.get("low_guard_loss_db"),
                "low_guard_loss_R_db": right_result.get("low_guard_loss_db"),
                "low_guard_penalty_L": left_result.get("low_guard_penalty"),
                "low_guard_penalty_R": right_result.get("low_guard_penalty"),
                "low_guard_reference": _combined_low_guard_reference(left_result, right_result),
                "low_guard_reference_L": left_result.get("low_guard_reference"),
                "low_guard_reference_R": right_result.get("low_guard_reference"),
                "score_L": round(score_left, 4),
                "score_L_pct": round(score_left * 100.0, 1),
                "score_R": round(score_right, 4),
                "score_R_pct": round(score_right * 100.0, 1),
                "scan": result.get("scan", "combined_matrix"),
                "score_source": "lr_combined",
            })
        if not combined_results:
            raise ValueError("No matching L/R AutoSub 2.2 matrix scoring results")
        combined_results.sort(key=lambda r: r["score"], reverse=True)
        return _finalize_matrix_scoring(
            combined_results,
            score_mode="lr_combined_matrix",
            scored_candidates=[result for _, result in both_valid],
        )

    fallback_key = "points_left"
    channel_name = "left"
    fallback = [(idx, result) for idx, result in indexed if _auto_sub_has_points(result, fallback_key)]
    right_fallback = [(idx, result) for idx, result in indexed if _auto_sub_has_points(result, "points_right")]
    if len(right_fallback) > len(fallback):
        fallback_key = "points_right"
        channel_name = "right"
        fallback = right_fallback
    if not fallback:
        raise ValueError("No valid AutoSub 2.2 matrix sweep results to score")

    fallback_reference_idx, fallback_reference_label = _reference_index(fallback, fallback_key)
    single_scoring = score_sub_alignment_candidates(
        [_copy_for_score(result, idx, fallback_key, fallback_reference_idx, fallback_reference_label) for idx, result in fallback],
        crossover_hz=crossover_hz,
    )
    by_idx = {idx: result for idx, result in fallback}
    matrix_results = []
    for scored in single_scoring["results"]:
        idx = int(round(float(scored.get("delay_ms", 0.0))))
        measured = by_idx.get(idx) or {}
        sub1_alignment = _auto_sub_clamped_delay(float(measured.get("sub1_alignment_ms", 0.0) or 0.0))
        sub2_alignment = _auto_sub_clamped_delay(float(measured.get("sub2_alignment_ms", 0.0) or 0.0))
        score = round(float(scored.get("score", 0.0) or 0.0), 4)
        score_pct = round(score * 100.0, 1)
        matrix_result = {
            "delay_ms": sub1_alignment,
            "sub1_alignment_ms": sub1_alignment,
            "sub2_alignment_ms": sub2_alignment,
            "incumbent_pair": _is_incumbent_pair(measured),
            "name": measured.get("name") or _auto_sub_22_name(sub1_alignment, sub2_alignment),
            "score": score,
            "score_pct": score_pct,
            "xo_score": scored.get("xo_score"),
            "timing_band_score": scored.get("timing_band_score"),
            "low_guard_loss_db": scored.get("low_guard_loss_db"),
            "low_guard_penalty": scored.get("low_guard_penalty"),
            "low_guard_reference": scored.get("low_guard_reference"),
            "final_score": scored.get("final_score", score),
            "scan": measured.get("scan", "combined_matrix"),
            "score_source": f"{channel_name}_fallback",
        }
        if channel_name == "left":
            matrix_result.update({"score_L": score, "score_L_pct": score_pct, "score_R": None, "score_R_pct": None})
        else:
            matrix_result.update({"score_L": None, "score_L_pct": None, "score_R": score, "score_R_pct": score_pct})
        matrix_results.append(matrix_result)
    return _finalize_matrix_scoring(
        matrix_results,
        score_mode=f"{channel_name}_fallback_matrix",
        scored_candidates=[result for _, result in fallback],
    )

def _auto_sub_candidate_ledger(
    candidates: list[dict[str, Any]],
    scoring: dict[str, Any],
    *,
    mode: str,
    phase: str,
    channel: str | None = None,
    roles: dict[str, dict[str, Any] | None] | None = None,
    decision_pool: list[dict[str, Any]] | None = None,
    requested_incumbent: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Describe scorer decisions without participating in them.

    This deliberately runs only after a scorer has returned.  It reconstructs
    inclusion from the scorer output and never supplies data back to scoring.
    """
    score_mode = str(scoring.get("score_mode") or "")
    matrix = phase == "matrix"

    def key(result: dict[str, Any]) -> tuple[float, ...]:
        if matrix:
            return (
                round(float(result.get("sub1_alignment_ms", 0.0) or 0.0), 2),
                round(float(result.get("sub2_alignment_ms", 0.0) or 0.0), 2),
            )
        return (round(float(result.get("delay_ms", 0.0) or 0.0), 2),)

    scored_by_key = {key(result): result for result in scoring.get("results", [])}
    complete_available = sum(
        1 for result in (decision_pool if decision_pool is not None else candidates)
        if _auto_sub_has_points(result, "points_left") and _auto_sub_has_points(result, "points_right")
    ) >= 2
    majority_channel = "left" if score_mode.startswith("left_") else "right" if score_mode.startswith("right_") else None
    role_keys = {
        name: key(result) for name, result in (roles or {}).items() if isinstance(result, dict)
    }
    if requested_incumbent is not None:
        role_keys["incumbent"] = key(requested_incumbent)
    ledger: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_key = key(candidate)
        left_count = len(candidate.get("points_left") or (candidate.get("points") if channel == "left" else []) or [])
        right_count = len(candidate.get("points_right") or (candidate.get("points") if channel == "right" else []) or [])
        if channel == "left":
            eligible = left_count >= 3
        elif channel == "right":
            eligible = right_count >= 3
        else:
            eligible = left_count >= 3 or right_count >= 3
        scored = scored_by_key.get(candidate_key)
        included = scored is not None
        reason = None
        marked_reason = candidate.get("exclusion_reason")
        if isinstance(marked_reason, str) and marked_reason:
            # Explicit upstream exclusion (candidate plausibility gate): the
            # row never entered the scoring input, so report that reason.
            reason = str(marked_reason)
            included = False
        elif not eligible:
            if channel == "left":
                reason = "left_insufficient_points"
            elif channel == "right":
                reason = "right_insufficient_points"
            elif left_count < 3 and right_count < 3:
                reason = "both_insufficient_points"
            elif left_count < 3:
                reason = "left_insufficient_points"
            else:
                reason = "right_insufficient_points"
        elif not included:
            left_ok = left_count >= 3
            right_ok = right_count >= 3
            if complete_available and not (left_ok and right_ok):
                reason = "single_side_excluded_because_complete_candidates_available"
            elif majority_channel and not ((majority_channel == "left" and left_ok) or (majority_channel == "right" and right_ok)):
                reason = "excluded_by_majority_side_fallback"
            else:
                reason = "delay_key_merge_failed"
        row: dict[str, Any] = {
            "mode": mode,
            "phase": phase,
            "requested_delay_ms": candidate.get("delay_ms"),
            "requested_sub1_alignment_ms": candidate.get("sub1_alignment_ms"),
            "requested_sub2_alignment_ms": candidate.get("sub2_alignment_ms"),
            "delay_ms": candidate.get("delay_ms"),
            "sub1_alignment_ms": candidate.get("sub1_alignment_ms"),
            "sub2_alignment_ms": candidate.get("sub2_alignment_ms"),
            "status_left": candidate.get("status") if channel == "left" else candidate.get("status_left"),
            "status_right": candidate.get("status") if channel == "right" else candidate.get("status_right"),
            "points_left": left_count,
            "points_right": right_count,
            "eligible_for_scoring": eligible,
            "included_in_scoring": included,
            "exclusion_reason": reason,
            "plausibility": candidate.get("plausibility"),
            "score": scored.get("score") if scored else None,
            "final_score": scored.get("final_score", scored.get("score")) if scored else None,
            "score_pct": scored.get("score_pct") if scored else None,
            "score_left": scored.get("score_L") if scored else None,
            "score_right": scored.get("score_R") if scored else None,
            "roles": sorted(name for name, role_key in role_keys.items() if role_key == candidate_key),
        }
        ledger.append(row)
        logger.info("AUTOSUB_CANDIDATE %s", json.dumps(row, sort_keys=True, separators=(",", ":")))
    return ledger
