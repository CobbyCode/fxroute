# SPDX-License-Identifier: AGPL-3.0-only

"""AutoSub subwoofer alignment optimization: jobs, sweeps, scoring, gain."""

import asyncio
import copy
import json
import logging
import math
import statistics
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from measurement import score_sub_alignment_candidates
from uploads import UploadTooLargeError, read_upload
from samplerate import (
    OUTPUT_MODE_SUBWOOFER_21,
    OUTPUT_MODE_SUBWOOFER_22,
    OUTPUT_MODE_SUBWOOFER_22_STEREO,
    OUTPUT_MODE_SUBWOOFER_22_MODES,
    OUTPUT_MODE_SUBWOOFER_MODES,
    get_audio_output_overview,
    set_audio_output_mode,
)
from subwoofer_runtime import SubwooferRuntimeConfig

logger = logging.getLogger(__name__)

_AUTO_SUB_JOBS: dict[str, dict[str, Any]] = {}
_auto_sub_lock: asyncio.Lock = asyncio.Lock()
_AUTO_SUB_WORKER_TASKS: set[asyncio.Task[Any]] = set()
_AUTO_SUB_CLEANUP_TASKS: set[asyncio.Task[Any]] = set()
_AUTO_SUB_MAX_CALIBRATION_BYTES: int = 2 * 1024 * 1024  # 2 MiB

router = APIRouter()
# ---------------------------------------------------------------------------


def is_optimization_active() -> bool:
    return bool(_auto_sub_lock and _auto_sub_lock.locked())


def _cleanup_stale_autosub_cancelling_jobs() -> None:
    """Promote stale cancelling AutoSub jobs when the lock is not held.

    If the lock can be acquired immediately, no worker is actively running,
    so any cancelling job is stale and can be marked cancelled.
    """
    try:
        acquired = _auto_sub_lock and _auto_sub_lock.locked()
    except RuntimeError:
        acquired = False
    if acquired:
        return  # Lock held, worker still active

    for job_id, job in list(_AUTO_SUB_JOBS.items()):
        if str(job.get("status") or "").lower() != "cancelling":
            continue
        logger.warning(
            "AUTOSUB stale running state recovered: job_id=%s status=cancelling->cancelled",
            job_id,
        )
        job["status"] = "cancelled"
        job["message"] = "Auto Sub Optimize cancelled."
        job["cancel_requested"] = True
        job["cancelled_at"] = job.get("cancelled_at") or datetime.now(timezone.utc).isoformat()


def _auto_sub_cancel_requested(job: dict[str, Any]) -> bool:
    status = str(job.get("status") or "").lower()
    return bool(job.get("cancel_requested")) or status in ("cancelled", "cancelling")


def _start_auto_sub_worker(coro) -> None:
    task = asyncio.create_task(coro)
    _AUTO_SUB_WORKER_TASKS.add(task)
    task.add_done_callback(_AUTO_SUB_WORKER_TASKS.discard)


async def shutdown() -> None:
    """Cooperatively cancel and drain all AutoSub-owned tasks."""
    from main import measurement_store

    for job in _AUTO_SUB_JOBS.values():
        if str(job.get("status") or "") not in {"completed", "failed", "cancelled"}:
            job["status"] = "cancelling"
            job["cancel_requested"] = True
            sweep_id = str(job.get("current_sweep_id") or "")
            if sweep_id and measurement_store is not None:
                try:
                    measurement_store.cancel_job(sweep_id)
                except (KeyError, RuntimeError):
                    pass
    if _AUTO_SUB_WORKER_TASKS:
        await asyncio.gather(*list(_AUTO_SUB_WORKER_TASKS), return_exceptions=True)
    cleanup_tasks = list(_AUTO_SUB_CLEANUP_TASKS)
    for task in cleanup_tasks:
        task.cancel()
    if cleanup_tasks:
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)


def _auto_sub_cancelled_candidate(delay_ms: float, stage: str) -> dict[str, Any]:
    return {
        "delay_ms": delay_ms,
        "name": str(delay_ms),
        "points": [],
        "sweep_id": "",
        "status": "cancelled",
        "error": "Auto Sub Optimize cancelled",
        "scan": stage,
    }


async def _restore_auto_sub_original_config(original_config_snapshot: dict[str, Any]) -> None:
    """Restore subwoofer config from snapshot."""
    from main import subwoofer_runtime
    try:
        from samplerate import set_audio_output_mode
        mode = original_config_snapshot.get("mode", "stereo") or "stereo"
        subwoofer_config = (
            _auto_sub_22_global_config(original_config_snapshot)
            if mode in OUTPUT_MODE_SUBWOOFER_22_MODES
            else original_config_snapshot.get("subwoofer") or {}
        )
        set_audio_output_mode(
            mode,
            subwoofer_config,
            original_config_snapshot.get("subwoofers") or {},
        )
        if subwoofer_runtime is not None:
            config = SubwooferRuntimeConfig.from_overview(get_audio_output_overview())
            await subwoofer_runtime.sync(config)
    except Exception:
        logger.exception("Auto-sub: failed to restore original config from snapshot")


def _auto_sub_step_ms(fc: int) -> float:
    return (1000.0 / float(fc)) / 16.0


def _auto_sub_clamped_delay(delay_ms: float) -> float:
    return round(max(-40.0, min(40.0, float(delay_ms))), 2)


def _auto_sub_sweep_profile(fc: float) -> dict[str, float]:
    """Build the bass-focused AutoSub sweep profile for a crossover frequency."""
    auto_sub_sweep_low_hz = 20.0
    auto_sub_sweep_high_hz = max(600.0, min(float(fc) * 8.0, 2000.0))
    if fc <= 60:
        auto_sub_sweep_sec, auto_sub_tail_sec = 3.5, 1.5
    elif fc <= 120:
        auto_sub_sweep_sec, auto_sub_tail_sec = 3.0, 1.3
    else:
        auto_sub_sweep_sec, auto_sub_tail_sec = 2.5, 1.1
    return {
        "sweep_start_hz": auto_sub_sweep_low_hz,
        "sweep_end_hz": auto_sub_sweep_high_hz,
        "sweep_seconds": auto_sub_sweep_sec,
        "tail_seconds": auto_sub_tail_sec,
    }


def _auto_sub_snapshot_copy(mode_state: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(mode_state))
    except Exception:
        return dict(mode_state)


def _auto_sub_22_global_config(snapshot: dict[str, Any]) -> dict[str, Any]:
    subwoofer = snapshot.get("subwoofer") if isinstance(snapshot.get("subwoofer"), dict) else {}
    return {
        "crossover_frequency_hz": snapshot.get("crossover_frequency_hz", subwoofer.get("crossover_frequency_hz", 80)),
        "main_highpass_enabled": snapshot.get("main_highpass_enabled", subwoofer.get("main_highpass_enabled", True)),
    }


def _auto_sub_22_sub(snapshot: dict[str, Any], sub_key: str) -> dict[str, Any]:
    subwoofers = snapshot.get("subwoofers") if isinstance(snapshot.get("subwoofers"), dict) else {}
    sub = subwoofers.get(sub_key) if isinstance(subwoofers.get(sub_key), dict) else {}
    return {
        "level_db": float(sub.get("level_db", 0.0) or 0.0),
        "alignment_ms": _auto_sub_clamped_delay(float(sub.get("alignment_ms", 0.0) or 0.0)),
        "polarity": str(sub.get("polarity", "normal") or "normal"),
    }


def _auto_sub_22_candidate_subwoofers(
    snapshot: dict[str, Any],
    *,
    sub1_alignment_ms: float,
    sub2_alignment_ms: float,
    active_subs: tuple[str, ...],
    sub1_polarity: str | None = None,
    sub2_polarity: str | None = None,
) -> dict[str, Any]:
    sub1 = _auto_sub_22_sub(snapshot, "sub1")
    sub2 = _auto_sub_22_sub(snapshot, "sub2")
    sub1["alignment_ms"] = _auto_sub_clamped_delay(sub1_alignment_ms)
    sub2["alignment_ms"] = _auto_sub_clamped_delay(sub2_alignment_ms)
    if sub1_polarity is not None:
        sub1["polarity"] = "invert" if sub1_polarity == "invert" else "normal"
    if sub2_polarity is not None:
        sub2["polarity"] = "invert" if sub2_polarity == "invert" else "normal"
    if "sub1" not in active_subs:
        sub1["level_db"] = -80.0
    if "sub2" not in active_subs:
        sub2["level_db"] = -80.0
    return {"sub1": sub1, "sub2": sub2}


def _auto_sub_22_verify_alignment(mode_state: dict[str, Any], sub1_alignment_ms: float, sub2_alignment_ms: float) -> bool:
    subwoofers = mode_state.get("subwoofers") if isinstance(mode_state.get("subwoofers"), dict) else {}
    sub1 = subwoofers.get("sub1") if isinstance(subwoofers.get("sub1"), dict) else {}
    sub2 = subwoofers.get("sub2") if isinstance(subwoofers.get("sub2"), dict) else {}
    try:
        return (
            abs(float(sub1.get("alignment_ms", -9999)) - _auto_sub_clamped_delay(sub1_alignment_ms)) <= 0.001
            and abs(float(sub2.get("alignment_ms", -9999)) - _auto_sub_clamped_delay(sub2_alignment_ms)) <= 0.001
        )
    except (TypeError, ValueError):
        return False


def _auto_sub_opposite_polarity(polarity: str) -> str:
    return "normal" if str(polarity).lower() == "invert" else "invert"


def _auto_sub_polarity_decision(
    incumbent: dict[str, Any], alternative: dict[str, Any], *, min_score_gain: float = 0.03,
) -> dict[str, Any]:
    """Protect the active polarity unless a measured alternative is clearly better."""
    incumbent_score = _auto_sub_score_value(incumbent)
    alternative_score = _auto_sub_score_value(alternative)
    gain = alternative_score - incumbent_score
    accepted = gain >= min_score_gain
    return {
        "accepted": accepted,
        "score_gain": round(gain, 4),
        "min_score_gain": min_score_gain,
        "reason": "alternative_clearly_better" if accepted else "incumbent_protected_unclear_advantage",
    }


def _auto_sub_22_name(sub1_alignment_ms: float, sub2_alignment_ms: float) -> str:
    return f"Sub1 {sub1_alignment_ms:.2f} ms / Sub2 {sub2_alignment_ms:.2f} ms"


def _auto_sub_22_stereo_name(left_alignment_ms: float, right_alignment_ms: float) -> str:
    return f"Left {left_alignment_ms:.2f} ms / Right {right_alignment_ms:.2f} ms"


def _auto_sub_direct_neighbors(delay_a: float, delay_b: float, scan_delays: list[float]) -> bool:
    sorted_delays = sorted(float(delay) for delay in scan_delays)
    tolerance = 0.05
    for left, right in zip(sorted_delays, sorted_delays[1:]):
        if abs(left - float(delay_a)) <= tolerance and abs(right - float(delay_b)) <= tolerance:
            return True
        if abs(right - float(delay_a)) <= tolerance and abs(left - float(delay_b)) <= tolerance:
            return True
    return False


def _auto_sub_fine_delay_candidates(
    winner: dict[str, Any],
    runner_up: dict[str, Any] | None,
    step_ms: float,
    existing_delays: set[float],
) -> list[float]:
    """Generate 4-6 fine delays around the coarse winner area."""
    winner_delay = float(winner.get("delay_ms", 0.0))
    fine_step = step_ms / 4.0
    offsets: list[float] = []

    # Always sample winner +/- 0.25 and +/- 0.5 coarse step.
    offsets.extend([-2.0 * fine_step, -fine_step, fine_step, 2.0 * fine_step])

    if runner_up is not None:
        runner_delay = float(runner_up.get("delay_ms", winner_delay))
        delta = runner_delay - winner_delay
        if 0.05 < abs(delta) <= (step_ms + 0.05):
            # Cover the interval and the runner-up neighbourhood without
            # exceeding the 4-6 candidate target after de-duplication.
            offsets.extend([
                delta * 0.5,
                delta - fine_step,
                delta + fine_step,
                delta * 0.25,
                delta * 0.75,
            ])

    candidates: list[float] = []
    existing = {round(float(delay), 2) for delay in existing_delays}
    for offset in sorted(offsets, key=lambda value: (abs(value), value)):
        delay = _auto_sub_clamped_delay(winner_delay + offset)
        if any(abs(delay - existing_delay) <= 0.05 for existing_delay in existing):
            continue
        if all(abs(delay - candidate) > 0.05 for candidate in candidates):
            candidates.append(delay)
            existing.add(round(delay, 2))
        if len(candidates) >= 6:
            break

    return candidates


def _auto_sub_fine_trigger_reasons(
    scoring: dict[str, Any],
    scan_delays: list[float],
) -> list[str]:
    reasons: list[str] = []
    winner = scoring.get("winner") or {}
    runner_up = scoring.get("runner_up")

    if scoring.get("confidence") == "uncertain":
        reasons.append("uncertain coarse confidence")

    if runner_up:
        winner_score = float(winner.get("score_pct", 0.0) or 0.0)
        runner_score = float(runner_up.get("score_pct", 0.0) or 0.0)
        if winner_score - runner_score < 5.0:
            reasons.append("winner/runner-up margin below 5 percentage points")
        if _auto_sub_direct_neighbors(
            float(winner.get("delay_ms", 0.0)),
            float(runner_up.get("delay_ms", 0.0)),
            scan_delays,
        ):
            reasons.append("winner and runner-up are direct coarse neighbours")

    return reasons


def _auto_sub_rank_results(results: list[dict[str, Any]]) -> None:
    for rank, result in enumerate(results, start=1):
        result["rank"] = rank


def _auto_sub_has_points(result: dict[str, Any], key: str = "points") -> bool:
    points = result.get(key) or []
    return isinstance(points, list) and len(points) >= 3


def _auto_sub_delay_key(result: dict[str, Any]) -> float:
    return round(float(result.get("delay_ms", 0.0)), 2)


def _auto_sub_score_value(result: dict[str, Any] | None) -> float:
    if not result:
        return float("-inf")
    try:
        return float(result.get("final_score", result.get("score", 0.0)) or 0.0)
    except (TypeError, ValueError):
        return float("-inf")


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


def _auto_sub_measurement_from_sweep(
    sweep_result: dict[str, Any],
    label: str,
    name: str,
    offset_db: float | None = None,
) -> dict[str, Any]:
    """Convert an AutoSub sweep result into a frontend-compatible measurement dict.

    When *offset_db* is provided it is used as the shared vertical reference
    for all traces.  When omitted, the shared offset is computed from the
    combined bass-region (20‑200 Hz) dB values of the L + R points inside
    *sweep_result*, which is the correct default for a single-sweep call
    (2.1 / 2.2 Mono).  Callers that need a cross-sweep shared offset
    (2.2 Stereo) can pre-compute it and pass it explicitly.
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

    return {
        "id": f"autosub-{base_id}",
        "name": name,
        "traces": traces,
    }


def _auto_sub_select_accepted_winner(
    *,
    coarse_winner: dict[str, Any],
    fine_winner: dict[str, Any] | None,
    incumbent_winner: dict[str, Any] | None,
    score_epsilon: float = 0.001,
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
            if matrix_score <= incumbent_score:
                accepted_winner = incumbent_winner
                incumbent_accepted = True
                reject_reason = "incumbent_better"

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
        if not eligible:
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


def _capture_auto_sub_playback_gain() -> dict[str, Any]:
    """Capture one fixed neutral source gain for an AutoSub optimization job."""
    from main import easyeffects_manager
    manager = easyeffects_manager
    loudness_enabled = False
    volume_db = 0.0
    if manager is not None:
        try:
            extras = manager.load_global_extras()
            loudness = extras.get("loudness") if isinstance(extras, dict) else {}
            loudness = loudness if isinstance(loudness, dict) else {}
            loudness_enabled = bool(loudness.get("enabled"))
            if loudness_enabled:
                volume_db = float(loudness.get("params", {}).get("volumeDb", 0.0))
        except Exception as exc:
            raise RuntimeError("Could not capture the EasyEffects Loudness volume for AutoSub") from exc
    if not math.isfinite(volume_db):
        raise RuntimeError("EasyEffects Loudness volume for AutoSub is not finite")
    playback_gain = 10.0 ** (volume_db / 20.0) if loudness_enabled else 1.0
    if not math.isfinite(playback_gain) or playback_gain < 0.0:
        raise RuntimeError("AutoSub playback gain is not finite")
    return {
        "enabled": loudness_enabled,
        "volume_db": volume_db if loudness_enabled else 0.0,
        "linear": playback_gain,
        "source": "loudness.params.volumeDb" if loudness_enabled else "hardware-sink",
    }


def _auto_sub_job_playback_gain(job: Mapping[str, Any]) -> float:
    """Read the immutable per-job source gain, defaulting old jobs to unity."""
    payload = job.get("playback_gain")
    value = payload.get("linear", 1.0) if isinstance(payload, Mapping) else (1.0 if payload is None else payload)
    try:
        playback_gain = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("AutoSub job playback gain is invalid") from exc
    if not math.isfinite(playback_gain) or playback_gain < 0.0:
        raise RuntimeError("AutoSub job playback gain is invalid")
    return playback_gain


@router.post("/api/measurements/auto-sub-optimize/start")
async def start_auto_sub_optimize(
    input_id: str = Form(...),
    input_key: str = Form(""),
    channel: str = Form("left"),
    mic_input_channel: str = Form("1"),
    reference_input_channel: str = Form(""),
    calibration_ref: str = Form(""),
    target_curve_snapshot: str = Form(""),
    calibration_file: UploadFile | None = File(None),
):
    from main import measurement_store, measurement_sr_session, subwoofer_runtime
    global _auto_sub_lock
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        input_id = measurement_store.resolve_capture_input_id(input_id=input_id, input_key=input_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not _auto_sub_lock:
        _auto_sub_lock = asyncio.Lock()

    # Capture the session entry epoch before any await can interleave a
    # measurement-window close: an entry invalidated by a close must never
    # register as a running Auto-Sub job afterwards.
    entry_epoch = (
        measurement_sr_session.capture_entry_epoch()
        if measurement_sr_session is not None
        else None
    )

    from samplerate import _load_audio_output_mode, set_audio_output_mode

    # Reject if any measurement is already running
    if measurement_store.has_active_measurement_job():
        raise HTTPException(status_code=409, detail="Another measurement is already running")

    # Clean up stale cancelling jobs before lock acquisition
    _cleanup_stale_autosub_cancelling_jobs()

    # Acquire lock before modifying AutoSub state
    try:
        await asyncio.wait_for(_auto_sub_lock.acquire(), timeout=0.5)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=423, detail="Auto Sub Optimize is already in progress")

    try:
        mode_state = _load_audio_output_mode()
        output_mode = mode_state.get("mode")
        if output_mode not in OUTPUT_MODE_SUBWOOFER_MODES:
            raise HTTPException(status_code=400, detail="Auto Sub Optimize requires 2.1 or 2.2 Subwoofer output mode")
        auto_sub_playback_gain = _capture_auto_sub_playback_gain()

        if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
            sub1 = _auto_sub_22_sub(mode_state, "sub1")
            sub2 = _auto_sub_22_sub(mode_state, "sub2")
            fc = int(mode_state.get("crossover_frequency_hz", 80))
            current_alignment = float(sub1.get("alignment_ms", 0.0))
            current_sub2_alignment = float(sub2.get("alignment_ms", 0.0))
            original_polarity = str(sub1.get("polarity", "normal"))
            original_level = float(sub1.get("level_db", 0.0))
            original_highpass = bool(mode_state.get("main_highpass_enabled", True))
        else:
            sub = mode_state.get("subwoofer") or {}
            fc = int(sub.get("crossover_frequency_hz", 80))
            current_alignment = float(sub.get("sub_alignment_ms", 0.0))
            current_sub2_alignment = 0.0
            original_polarity = str(sub.get("sub_polarity", "normal"))
            original_level = float(sub.get("sub_level_db", 0.0))
            original_highpass = bool(sub.get("main_highpass_enabled", True))

        # Compute scan range
        step_ms = _auto_sub_step_ms(fc)
        coarse_steps = 4
        scan_delays: list[float] = []
        for s in range(-coarse_steps, coarse_steps + 1):
            delay = _auto_sub_clamped_delay(current_alignment + s * step_ms)
            if not scan_delays or abs(delay - scan_delays[-1]) > 0.05:
                scan_delays.append(delay)

        # Snapshot original config for rollback
        original_config_snapshot = _auto_sub_snapshot_copy(mode_state)
        target_curve, target_curve_error = _validate_auto_sub_target_curve_snapshot(target_curve_snapshot)

        job_id = f"auto-sub-{uuid4().hex[:12]}"
        job: dict[str, Any] = {
            "id": job_id,
            "status": "preparing",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "message": f"Auto Sub Optimize: {len(scan_delays)} candidates @ {fc} Hz",
            "result": None,
            "error": None,
            "mode": output_mode,
            "crossover_hz": fc,
            "scan_delays": scan_delays,
            "step_ms": step_ms,
            "original_alignment_ms": current_alignment,
            "original_sub1_alignment_ms": current_alignment,
            "original_sub2_alignment_ms": current_sub2_alignment,
            "original_config_snapshot": original_config_snapshot,
            "target_curve": target_curve,
            "auto_gain": {
                "available": False,
                "reason": target_curve_error or "Vertical Main/Target level reference has not passed its mandatory gate",
            },
            "playback_gain": auto_sub_playback_gain,
            "current_sweep_id": "",
            "cancel_requested": False,
            "cancelled_at": None,
            "fine_scan": {
                "enabled": False,
                "triggered": False,
                "status": "pending",
                "candidates": [],
            },
        }
        _AUTO_SUB_JOBS[job_id] = job
        logger.info(
            "AUTOSUB job=%s start mode=%s fc=%sHz candidates=%s target_curve=%s auto_gain=%s playback_gain=%s",
            job_id, output_mode, fc, len(scan_delays),
            json.dumps(target_curve, sort_keys=True, separators=(",", ":")) if target_curve else "unavailable",
            json.dumps(job["auto_gain"], sort_keys=True, separators=(",", ":")),
            json.dumps(auto_sub_playback_gain, sort_keys=True, separators=(",", ":")),
        )

        calibration_bytes = None
        calibration_filename = None
        if calibration_file is not None:
            calibration_filename = calibration_file.filename or "calibration.txt"
            content_type = str(calibration_file.content_type or "").lower()
            if "text" not in content_type and "plain" not in content_type and content_type not in ("", "application/octet-stream"):
                raise HTTPException(status_code=400, detail="Calibration file must be a text file")
            try:
                raw_bytes = await read_upload(calibration_file, _AUTO_SUB_MAX_CALIBRATION_BYTES)
            except UploadTooLargeError:
                raise HTTPException(status_code=400, detail=f"Calibration file too large (max {_AUTO_SUB_MAX_CALIBRATION_BYTES // (1024*1024)} MiB)")
            calibration_bytes = raw_bytes

        if output_mode == OUTPUT_MODE_SUBWOOFER_22_STEREO:
            fine_step_ms = step_ms / 4.0
            right_scan_delays: list[float] = []
            for s in range(-coarse_steps, coarse_steps + 1):
                delay = _auto_sub_clamped_delay(current_sub2_alignment + s * step_ms)
                if not right_scan_delays or abs(delay - right_scan_delays[-1]) > 0.05:
                    right_scan_delays.append(delay)
            job["message"] = (
                f"Auto Sub Optimize 2.2 Stereo Bass: Left Sub {len(scan_delays)} coarse, "
                f"Left fine up to 6, Right Sub {len(right_scan_delays)} coarse, Right fine up to 6 @ {fc} Hz"
            )
            job["scan_delays"] = {"left_sub": scan_delays, "right_sub": right_scan_delays}
            job["fine_scan"] = {
                "enabled": True,
                "triggered": False,
                "status": "pending",
                "reason": "2.2 Stereo Bass optimizes Left and Right Sub separately with per-side fine scans",
                "fine_step_ms": fine_step_ms,
                "left": {"status": "pending", "candidates": []},
                "right": {"status": "pending", "candidates": []},
            }
            _start_auto_sub_worker(
                _run_auto_sub_22_stereo_optimize(
                    job_id=job_id,
                    input_id=input_id,
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    left_scan_delays=scan_delays,
                    right_scan_delays=right_scan_delays,
                    fc=fc,
                    original_config_snapshot=original_config_snapshot,
                    entry_epoch=entry_epoch,
                )
            )
        elif output_mode == OUTPUT_MODE_SUBWOOFER_22:
            fine_step_ms = step_ms / 4.0
            sub2_scan_delays: list[float] = []
            for s in range(-coarse_steps, coarse_steps + 1):
                delay = _auto_sub_clamped_delay(current_sub2_alignment + s * step_ms)
                if not sub2_scan_delays or abs(delay - sub2_scan_delays[-1]) > 0.05:
                    sub2_scan_delays.append(delay)
            job["message"] = (
                f"Auto Sub Optimize 2.2: Sub 1 {len(scan_delays)} coarse, "
                f"Sub 2 {len(sub2_scan_delays)} coarse, 3x3 matrix @ {fc} Hz"
            )
            job["scan_delays"] = {"sub1": scan_delays, "sub2": sub2_scan_delays}
            job["combined_matrix"] = {"status": "pending", "fine_step_ms": fine_step_ms, "candidates": []}
            _start_auto_sub_worker(
                _run_auto_sub_22_optimize(
                    job_id=job_id,
                    input_id=input_id,
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    sub1_scan_delays=scan_delays,
                    sub2_scan_delays=sub2_scan_delays,
                    fc=fc,
                    original_config_snapshot=original_config_snapshot,
                    fine_step_ms=fine_step_ms,
                    entry_epoch=entry_epoch,
                )
            )
        else:
            _start_auto_sub_worker(
                _run_auto_sub_optimize(
                    job_id=job_id,
                    input_id=input_id,
                    channel=channel,
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    scan_delays=scan_delays,
                    fc=fc,
                    current_alignment=current_alignment,
                    original_polarity=original_polarity,
                    original_level=original_level,
                    original_highpass=original_highpass,
                    original_config_snapshot=original_config_snapshot,
                    entry_epoch=entry_epoch,
                )
            )
        return {"status": "ok", "job": job}
    except HTTPException:
        _auto_sub_lock.release()
        raise
    except asyncio.CancelledError:
        _auto_sub_lock.release()
        raise
    except Exception:
        _auto_sub_lock.release()
        raise


@router.get("/api/measurements/auto-sub-optimize/jobs/{job_id}")
async def get_auto_sub_optimize_job(job_id: str):
    job = _AUTO_SUB_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Auto Sub Optimize job not found")
    return {"status": "ok", "job": job}


@router.post("/api/measurements/auto-sub-optimize/jobs/{job_id}/cancel")
async def cancel_auto_sub_optimize_job(job_id: str):
    from main import measurement_store
    job = _AUTO_SUB_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Auto Sub Optimize job not found")
    if str(job.get("status") or "").lower() in {"completed", "failed", "cancelled"}:
        return {"status": "ok", "job": job}

    job["cancel_requested"] = True
    job["cancelled_at"] = datetime.now(timezone.utc).isoformat()
    job["status"] = "cancelling"
    job["message"] = "Auto Sub Optimize cancelling..."
    job["error"] = None
    logger.info("AUTOSUB job=%s cancel requested", job_id)

    current_sweep_id = job.get("current_sweep_id")
    if current_sweep_id and measurement_store:
        try:
            measurement_store.cancel_job(str(current_sweep_id))
        except KeyError:
            pass
        except Exception as exc:
            logger.warning("Auto-sub: failed to cancel current sweep %s: %s", current_sweep_id, exc)

    return {"status": "ok", "job": job}


_AUTO_SUB_TIMING_MARKS = [
    "config_set",
    "config_verify",
    "pre_arm",
    "sweep_start",
    "sweep_poll_done",
    "release_start",
    "release_done",
]

_AUTO_SUB_STAGE_PEAK_LIMIT_DBFS = -1.0
_AUTO_SUB_STAGE_PEAK_MISMATCH_DB = 1.0


class AutoSubPeakSafetyError(RuntimeError):
    """Abort the complete AutoSub run after a Stage1 peak safety failure."""


def _auto_sub_stage_peak_prediction(
    *, sweep_profile: dict[str, Any], sample_rate: int, channel: str,
    config: SubwooferRuntimeConfig, playback_gain: float = 1.0,
) -> dict[str, Any]:
    """Run the known measurement PCM through the native Stage1 DSP topology."""
    rate = int(sample_rate)
    duration = float(sweep_profile["sweep_seconds"])
    count = max(2048, int(round(rate * duration)))
    t = np.arange(count, dtype=np.float64) / rate
    start_hz = float(sweep_profile["sweep_start_hz"])
    end_hz = float(sweep_profile["sweep_end_hz"])
    log_ratio = math.log(end_hz / start_hz)
    phase = 2.0 * math.pi * start_hz * duration / log_ratio * (np.exp(t * log_ratio / duration) - 1.0)
    sweep = np.sin(phase)
    fade_len = min(count // 8, max(64, int(round(rate * 0.01))))
    if fade_len > 1:
        sweep[:fade_len] *= np.linspace(0.0, 1.0, fade_len)
        sweep[-fade_len:] *= np.linspace(1.0, 0.0, fade_len)
    sweep *= 0.8 / max(float(np.max(np.abs(sweep))), 1e-12)
    try:
        source_gain = float(playback_gain)
    except (TypeError, ValueError) as exc:
        raise ValueError("playback_gain must be a finite non-negative number") from exc
    if not math.isfinite(source_gain) or source_gain < 0.0:
        raise ValueError("playback_gain must be a finite non-negative number")
    sweep *= source_gain
    zeros = np.zeros_like(sweep)
    left = sweep if channel in ("left", "stereo") else zeros
    right = sweep if channel in ("right", "stereo") else zeros

    def coefficients(kind: str) -> tuple[list[float], list[float]]:
        w0 = 2.0 * math.pi * config.crossover_frequency_hz / rate
        cos_w0, sin_w0 = math.cos(w0), math.sin(w0)
        alpha = sin_w0 / (2.0 * math.sqrt(0.5))
        a0 = 1.0 + alpha
        if kind == "low":
            b = [(1.0 - cos_w0) * 0.5 / a0, (1.0 - cos_w0) / a0,
                 (1.0 - cos_w0) * 0.5 / a0]
        else:
            b = [(1.0 + cos_w0) * 0.5 / a0, -(1.0 + cos_w0) / a0,
                 (1.0 + cos_w0) * 0.5 / a0]
        return b, [1.0, -2.0 * cos_w0 / a0, (1.0 - alpha) / a0]

    def lr24(signal: np.ndarray, kind: str, enabled: bool = True) -> np.ndarray:
        if not enabled:
            return signal.copy()
        b, a = coefficients(kind)
        def one_stage(values: np.ndarray) -> np.ndarray:
            output = np.empty_like(values)
            z1 = 0.0
            z2 = 0.0
            for index, value in enumerate(values):
                filtered = b[0] * value + z1
                z1 = b[1] * value - a[1] * filtered + z2
                z2 = b[2] * value - a[2] * filtered
                output[index] = filtered
            return output
        return one_stage(one_stage(signal))

    def delay(signal: np.ndarray, delay_ms: float) -> np.ndarray:
        samples = int(float(delay_ms) * rate / 1000.0 + 0.5)
        if samples <= 0:
            return signal
        return np.concatenate((np.zeros(samples), signal))[:signal.size]

    main_l = delay(lr24(left, "high", config.main_highpass_enabled), config.derived_main_delay_ms)
    main_r = delay(lr24(right, "high", config.main_highpass_enabled), config.derived_main_delay_ms)
    if config.bass_routing == "stereo":
        sub1_source, sub2_source = left, right
    else:
        sub1_source = sub2_source = (left + right) * 0.5
    sub1 = delay(lr24(sub1_source, "low"), config.derived_sub1_delay_ms)
    sub2 = delay(lr24(sub2_source, "low"), config.derived_sub2_delay_ms)
    sub1 *= (-1.0 if config.sub_polarity == "invert" else 1.0) * 10.0 ** (config.sub_level_db / 20.0)
    sub2 *= (-1.0 if config.sub2_polarity == "invert" else 1.0) * 10.0 ** (config.sub2_level_db / 20.0)
    peaks = {
        f"output_{index + 1}": float(np.max(np.abs(signal))) if signal.size else 0.0
        for index, signal in enumerate((main_l, main_r, sub1, sub2))
    }
    peak_dbfs = {key: round(20.0 * math.log10(max(value, 1e-12)), 3) for key, value in peaks.items()}
    return {
        "linear": peaks,
        "dbfs": peak_dbfs,
        "maximum_dbfs": max(peak_dbfs.values()),
        "limit_dbfs": _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS,
        "safe": max(peak_dbfs.values()) <= _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS,
        "playback_gain": source_gain,
    }


def _auto_sub_stage_peak_comparison(
    predicted: dict[str, Any], measured_linear: dict[str, float],
) -> dict[str, Any]:
    measured_dbfs = {
        key: round(20.0 * math.log10(max(float(value), 1e-12)), 3)
        for key, value in measured_linear.items()
    }
    differences = {
        key: round(measured_dbfs[key] - float(predicted["dbfs"][key]), 3)
        for key in measured_dbfs
        if max(measured_dbfs[key], float(predicted["dbfs"][key])) > -90.0
    }
    relevant = any(abs(value) > _AUTO_SUB_STAGE_PEAK_MISMATCH_DB for value in differences.values())
    return {
        "predicted": predicted,
        "measured": {"linear": measured_linear, "dbfs": measured_dbfs},
        "difference_db": differences,
        "tolerance_db": _AUTO_SUB_STAGE_PEAK_MISMATCH_DB,
        "relevant_mismatch": relevant,
        "measured_safe": max(measured_dbfs.values()) <= _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS,
    }


def _auto_sub_timing_durations(marks: dict[str, float]) -> dict[str, float]:
    durations: dict[str, float] = {}
    prev_key = "start"
    for key in _AUTO_SUB_TIMING_MARKS:
        if key in marks and prev_key in marks:
            durations[f"{prev_key}_to_{key}_ms"] = round((marks[key] - marks[prev_key]) * 1000, 1)
        if key in marks:
            prev_key = key
    if "start" in marks and prev_key in marks:
        durations["total_ms"] = round((marks[prev_key] - marks["start"]) * 1000, 1)
    return durations


def _append_auto_sub_sweep_timing(
    job: dict[str, Any],
    *,
    delay_ms: float,
    channel: str,
    candidate_index: int,
    candidate_current: int | None,
    stage: str,
    status: str,
    marks: dict[str, float],
) -> None:
    job.setdefault("_sweep_timings", []).append({
        "delay_ms": delay_ms,
        "channel": channel,
        "stage": stage,
        "durations": _auto_sub_timing_durations(marks),
        "candidate": candidate_current or candidate_index,
        "sweep_index": candidate_index,
        "status": status,
    })


def _log_auto_sub_timing_summary(job: dict[str, Any]) -> None:
    timing_log = job.get("_sweep_timings", [])
    if not timing_log:
        return

    def _sum_phase(phase: str) -> float:
        return sum((t.get("durations", {}) or {}).get(phase, 0) or 0 for t in timing_log)

    total_config = _sum_phase("start_to_config_set_ms")
    total_verify = _sum_phase("config_set_to_config_verify_ms")
    total_prearm = _sum_phase("config_verify_to_pre_arm_ms")
    total_sweep = _sum_phase("pre_arm_to_sweep_start_ms")
    total_poll = _sum_phase("sweep_start_to_sweep_poll_done_ms")
    total_release = _sum_phase("sweep_poll_done_to_release_start_ms")
    total_cleanup = _sum_phase("release_start_to_release_done_ms")
    total_all = _sum_phase("total_ms")

    logger.info(
        "Auto-sub timing summary: count=%d sweeps total=%.1fs "
        "config=%.1fs verify=%.1fs prearm=%.1fs sweep=%.1fs poll=%.1fs release=%.1fs cleanup=%.1fs "
        "idle=%.1fs",
        len(timing_log), total_all / 1000,
        total_config / 1000, total_verify / 1000, total_prearm / 1000,
        total_sweep / 1000, total_poll / 1000, total_release / 1000, total_cleanup / 1000,
        max(0, (total_all - total_config - total_verify - total_prearm - total_sweep - total_poll - total_release - total_cleanup)) / 1000,
    )

    l_timings = [t for t in timing_log if t.get("channel") == "left"]
    r_timings = [t for t in timing_log if t.get("channel") == "right"]
    if l_timings:
        l_avg = sum((t.get("durations", {}) or {}).get("total_ms", 0) or 0 for t in l_timings) / len(l_timings)
        r_avg = (
            sum((t.get("durations", {}) or {}).get("total_ms", 0) or 0 for t in r_timings) / len(r_timings)
            if r_timings
            else 0
        )
        logger.info("Auto-sub timing: L avg=%.1fms R avg=%.1fms", l_avg, r_avg)


async def _measure_auto_sub_candidate(
    *,
    delay_ms: float,
    job: dict[str, Any],
    candidate_index: int,
    total: int,
    stage: str,
    fc: int,
    input_id: str,
    channel: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    auto_sub_sweep_profile: dict[str, Any],
    auto_sub_rate: int,
    original_level: float,
    original_polarity: str,
    original_highpass: bool,
    measurement_label: str | None = None,
    candidate_current: int | None = None,
    candidate_total: int | None = None,
    measure_channel: str | None = None,
    output_mode: str = OUTPUT_MODE_SUBWOOFER_21,
    original_config_snapshot: dict[str, Any] | None = None,
    sub1_alignment_ms: float | None = None,
    sub2_alignment_ms: float | None = None,
    active_subs: tuple[str, ...] = ("sub1",),
    sub1_polarity: str | None = None,
    sub2_polarity: str | None = None,
    exact_sub_mute: bool = False,
) -> dict[str, Any]:
    """Measure one AutoSub delay candidate with the standard safety checks."""
    from main import measurement_store, subwoofer_runtime
    from measurement_session import _sync_subwoofer_runtime_for_measurement_sweep
    from samplerate import _load_audio_output_mode

    _marks = {"start": time.monotonic()}
    _timing_written = False

    def _return_candidate(result: dict[str, Any]) -> dict[str, Any]:
        nonlocal _timing_written
        if not _timing_written:
            _marks.setdefault("release_done", time.monotonic())
            _append_auto_sub_sweep_timing(
                job,
                delay_ms=delay_ms,
                channel=measure_channel or channel,
                candidate_index=candidate_index,
                candidate_current=candidate_current,
                stage=stage,
                status=str(result.get("status") or "unknown"),
                marks=_marks,
            )
            _timing_written = True
        return result

    if _auto_sub_cancel_requested(job):
        return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))

    label = "Fine-Scan" if stage == "fine" else "Coarse scan"
    job["status"] = "running"
    job["message"] = measurement_label or f"{label}: sweep {candidate_index}/{total} @ sub_alignment_ms={delay_ms:.2f} ms"
    job["progress"] = {
        "current": candidate_index,
        "total": total,
        "delay_ms": delay_ms,
        "stage": stage,
        "sweep_current": candidate_index,
        "sweep_total": total,
    }
    if candidate_current is not None and candidate_total is not None:
        job["progress"]["candidate_current"] = candidate_current
        job["progress"]["candidate_total"] = candidate_total
    if measure_channel:
        job["progress"]["channel"] = measure_channel

    config_success = False
    try:
        if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
            snapshot = original_config_snapshot or {}
            sub1_delay = _auto_sub_clamped_delay(sub1_alignment_ms if sub1_alignment_ms is not None else delay_ms)
            sub2_delay = _auto_sub_clamped_delay(sub2_alignment_ms if sub2_alignment_ms is not None else _auto_sub_22_sub(snapshot, "sub2").get("alignment_ms", 0.0))
            sub_config = _auto_sub_22_global_config(snapshot)
            subwoofers_config = _auto_sub_22_candidate_subwoofers(
                snapshot,
                sub1_alignment_ms=sub1_delay,
                sub2_alignment_ms=sub2_delay,
                active_subs=active_subs,
                sub1_polarity=sub1_polarity,
                sub2_polarity=sub2_polarity,
            )
            set_audio_output_mode(output_mode, sub_config, subwoofers_config)
        else:
            sub_config = {
                "crossover_frequency_hz": fc,
                "sub_alignment_ms": delay_ms,
                "sub_level_db": original_level,
                "sub_polarity": original_polarity,
                "main_highpass_enabled": original_highpass,
            }
            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, sub_config)
        if subwoofer_runtime is not None:
            config = SubwooferRuntimeConfig.from_overview(get_audio_output_overview())
            await subwoofer_runtime.sync(config)
        _marks["config_set"] = time.monotonic()
        await asyncio.sleep(0.5)
        if _auto_sub_cancel_requested(job):
            return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
        verify = _load_audio_output_mode()
        if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
            config_success = _auto_sub_22_verify_alignment(verify, sub1_delay, sub2_delay)
        else:
            config_success = float(verify.get("subwoofer", {}).get("sub_alignment_ms", -999)) == delay_ms
        if not config_success:
            await asyncio.sleep(0.15)
            if _auto_sub_cancel_requested(job):
                return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
            verify = _load_audio_output_mode()
            if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
                config_success = _auto_sub_22_verify_alignment(verify, sub1_delay, sub2_delay)
            else:
                config_success = float(verify.get("subwoofer", {}).get("sub_alignment_ms", -999)) == delay_ms
            if not config_success:
                await asyncio.sleep(0.5)
                if _auto_sub_cancel_requested(job):
                    return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
                verify = _load_audio_output_mode()
                if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
                    config_success = _auto_sub_22_verify_alignment(verify, sub1_delay, sub2_delay)
                else:
                    config_success = float(verify.get("subwoofer", {}).get("sub_alignment_ms", -999)) == delay_ms
        _marks["config_verify"] = time.monotonic()
    except Exception as exc:
        logger.warning("Auto-sub: failed to configure delay %.2f ms: %s", delay_ms, exc)

    if not config_success:
        logger.warning("Auto-sub: skipping candidate %.2f ms — config sync failed", delay_ms)
        return _return_candidate({
            "delay_ms": delay_ms,
            "name": str(delay_ms),
            "points": [],
            "sweep_id": "",
            "status": "config_failed",
            "error": "Subwoofer config sync failed",
            "scan": stage,
        })

    try:
        await _sync_subwoofer_runtime_for_measurement_sweep(auto_sub_rate)
        _marks["pre_arm"] = time.monotonic()
        if _auto_sub_cancel_requested(job):
            return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
    except Exception as exc:
        logger.exception("Auto-sub: pre-arm failed for delay %.2f ms", delay_ms)
        return _return_candidate({
            "delay_ms": delay_ms,
            "name": str(delay_ms),
            "points": [],
            "sweep_id": "",
            "status": "pre_arm_failed",
            "error": str(exc),
            "scan": stage,
        })
    playback_gain = _auto_sub_job_playback_gain(job)
    stage_peak_prediction = _auto_sub_stage_peak_prediction(
        sweep_profile=auto_sub_sweep_profile,
        sample_rate=auto_sub_rate,
        channel=channel,
        config=SubwooferRuntimeConfig.from_overview(get_audio_output_overview()),
        playback_gain=playback_gain,
    )
    if exact_sub_mute:
        for key in ("output_3", "output_4"):
            stage_peak_prediction["linear"][key] = 0.0
            stage_peak_prediction["dbfs"][key] = -240.0
        stage_peak_prediction["maximum_dbfs"] = max(stage_peak_prediction["dbfs"].values())
        stage_peak_prediction["safe"] = stage_peak_prediction["maximum_dbfs"] <= _AUTO_SUB_STAGE_PEAK_LIMIT_DBFS
    if not stage_peak_prediction["safe"]:
        logger.error(
            "Auto-sub: blocked unsafe sweep candidate stage=%s delay=%.2f predicted=%s",
            stage, delay_ms, stage_peak_prediction["dbfs"],
        )
        job["message"] = (
            f"AutoGain candidate blocked before sweep: predicted Stage peak "
            f"{stage_peak_prediction['maximum_dbfs']:.2f} dBFS exceeds −1 dBFS"
        )
        peak_failure = {"predicted": stage_peak_prediction, "status": "headroom_blocked"}
        job.setdefault("auto_gain", {})["stage_output_peaks"] = peak_failure
        raise AutoSubPeakSafetyError(job["message"])
    sweep_id = ""
    stage_peak_comparison: dict[str, Any] | None = None
    previous_exact_sub_mute = False
    exact_sub_mute_enabled = False
    try:
        if exact_sub_mute:
            if subwoofer_runtime is None:
                raise RuntimeError("Subwoofer runtime unavailable; exact digital mute cannot be enabled")
            previous_exact_sub_mute = await subwoofer_runtime.set_exact_sub_mute(True)
            exact_sub_mute_enabled = True
            if not subwoofer_runtime.snapshot().get("exact_sub_mute"):
                raise RuntimeError("Subwoofer helper did not retain exact digital mute state")
        if subwoofer_runtime is None:
            raise RuntimeError("Native Stage1 output peak capture unavailable")
        await subwoofer_runtime.reset_output_peaks()
        sweep_job = await measurement_store.start_measurement(
            input_id=input_id,
            channel=channel,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref,
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            sweep_profile=auto_sub_sweep_profile,
            measurement_scope="raw_helper",
            playback_gain=playback_gain,
        )
        sweep_id = sweep_job["id"]
        job["current_sweep_id"] = sweep_id
        _marks["sweep_start"] = time.monotonic()

        if _auto_sub_cancel_requested(job):
            try:
                measurement_store.cancel_job(sweep_id)
            except Exception:
                pass
            job["current_sweep_id"] = ""
            return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))

        sweep_ok = False
        for _poll in range(120):
            if _auto_sub_cancel_requested(job):
                try:
                    measurement_store.cancel_job(sweep_id)
                except Exception:
                    pass
                if job.get("current_sweep_id") == sweep_id:
                    job["current_sweep_id"] = ""
                return _return_candidate(_auto_sub_cancelled_candidate(delay_ms, stage))
            await asyncio.sleep(0.5)
            try:
                current = measurement_store.get_job(sweep_id)
            except KeyError:
                sweep_ok = True
                break
            if current.get("status") in ("completed", "failed", "cancelled"):
                sweep_ok = True
                break

        if not sweep_ok:
            logger.warning("Auto-sub: sweep %s timed out (delay %.2f ms), cancelling", sweep_id, delay_ms)
            try:
                measurement_store.cancel_job(sweep_id)
            except Exception:
                pass
            await asyncio.sleep(0.5)

        _marks["sweep_poll_done"] = time.monotonic()
        measured_stage_peaks = await subwoofer_runtime.read_output_peaks()
        stage_peak_comparison = _auto_sub_stage_peak_comparison(
            stage_peak_prediction, measured_stage_peaks,
        )
        if stage_peak_comparison["relevant_mismatch"] or not stage_peak_comparison["measured_safe"]:
            reason = (
                "Native Stage1 peak mismatch"
                if stage_peak_comparison["relevant_mismatch"]
                else "Native Stage1 measured peak exceeded −1 dBFS"
            )
            logger.error("Auto-sub stopped: %s diagnostics=%s", reason, json.dumps(stage_peak_comparison, sort_keys=True))
            job["message"] = f"Auto Sub stopped: {reason}"
            job.setdefault("auto_gain", {})["stage_output_peaks"] = stage_peak_comparison
            if job.get("current_sweep_id") == sweep_id:
                job["current_sweep_id"] = ""
            raise AutoSubPeakSafetyError(reason)
        _marks["release_start"] = time.monotonic()
        _marks["release_done"] = time.monotonic()

        try:
            final = measurement_store.get_job(sweep_id)
        except KeyError:
            if job.get("current_sweep_id") == sweep_id:
                job["current_sweep_id"] = ""
            logger.warning("Auto-sub: sweep job disappeared after completion polling: %s", sweep_id)
            return _return_candidate({
                "delay_ms": delay_ms,
                "name": str(delay_ms),
                "points": [],
                "sweep_id": sweep_id,
                "status": "cancelled",
                "error": "Sweep job disappeared",
                "scan": stage,
            })
        if final.get("status") == "completed" and final.get("result"):
            result = final["result"]
            measurement = result.get("measurement") or {}
            points = []
            for t in (measurement.get("traces") or []):
                if t.get("kind") == "sweep-response":
                    points = t.get("points") or []
                    break
            if not points:
                for t in (measurement.get("review_traces") or []):
                    points = t.get("points") or []
                    if points:
                        break
            if not points:
                logger.warning("Auto-sub: no points in sweep result for delay %.2f ms", delay_ms)
            if job.get("current_sweep_id") == sweep_id:
                job["current_sweep_id"] = ""
            analysis = measurement.get("analysis") if isinstance(measurement.get("analysis"), dict) else {}
            normalized_by_db = analysis.get("normalized_by_db")
            calibrated_points = None
            if normalized_by_db is not None:
                calibrated_points = _auto_sub_reconstruct_calibrated_points(points, normalized_by_db)
            return _return_candidate({
                "delay_ms": delay_ms,
                "name": str(delay_ms),
                "points": points,
                "sweep_id": sweep_id,
                "status": "completed",
                "scan": stage,
                "normalized_by_db": normalized_by_db,
                "calibrated_points": calibrated_points,
                "exact_sub_mute": bool(exact_sub_mute),
                "measurement_channel": str(measurement.get("channel") or channel),
                "sample_rate": analysis.get("sample_rate"),
                "stage_output_peaks": stage_peak_comparison,
            })

        error_msg = final.get("error", {}).get("detail") if isinstance(final.get("error"), dict) else str(final.get("error") or "timeout")
        logger.warning("Auto-sub: sweep failed for delay %.2f ms: %s", delay_ms, error_msg)
        if job.get("current_sweep_id") == sweep_id:
            job["current_sweep_id"] = ""
        return _return_candidate({
            "delay_ms": delay_ms,
            "name": str(delay_ms),
            "points": [],
            "sweep_id": sweep_id,
            "status": "failed",
            "error": error_msg,
            "scan": stage,
            "stage_output_peaks": stage_peak_comparison or {"predicted": stage_peak_prediction},
        })
    except AutoSubPeakSafetyError:
        raise
    except Exception as exc:
        logger.exception("Auto-sub: sweep error for delay %.2f ms", delay_ms)
        if job.get("current_sweep_id") == sweep_id:
            job["current_sweep_id"] = ""
        return _return_candidate({
            "delay_ms": delay_ms,
            "name": str(delay_ms),
            "points": [],
            "sweep_id": sweep_id,
            "status": "error",
            "error": str(exc),
            "scan": stage,
        })
    finally:
        if exact_sub_mute_enabled and subwoofer_runtime is not None:
            try:
                await subwoofer_runtime.set_exact_sub_mute(previous_exact_sub_mute)
            except Exception as exc:
                logger.exception("Auto-sub: failed to restore exact sub mute after %s reference", channel)
                job["auto_gain"] = {
                    "available": False,
                    "reason": f"Exact sub mute restore failed after Main-only {channel}: {exc}",
                }
                raise RuntimeError(
                    f"AutoSub stopped: exact sub mute restoration was not acknowledged after Main-only {channel}"
                ) from exc


def _auto_sub_reconstruct_calibrated_points(
    normalized_points: list[list[float]], normalized_by_db: Any,
) -> list[list[float]]:
    """Undo MeasurementStore normalization: normalized = raw - normalized_by."""
    offset = float(normalized_by_db)
    if not math.isfinite(offset):
        raise ValueError("normalized_by_db must be finite")
    reconstructed: list[list[float]] = []
    for point in normalized_points:
        frequency_hz, normalized_db = float(point[0]), float(point[1])
        if not math.isfinite(frequency_hz) or frequency_hz <= 0 or not math.isfinite(normalized_db):
            raise ValueError("Measurement point must contain finite positive frequency and finite dB")
        reconstructed.append([frequency_hz, round(normalized_db + offset, 3)])
    return reconstructed


def _auto_sub_log_interpolate_points(
    points: list[list[float]], frequencies_hz: list[float],
) -> list[list[float]]:
    """Interpolate dB values linearly in log-frequency, without extrapolation."""
    source = [(float(point[0]), float(point[1])) for point in points]
    if len(source) < 2:
        raise ValueError("At least two interpolation points are required")
    if any(not math.isfinite(frequency) or frequency <= 0 or not math.isfinite(db) for frequency, db in source):
        raise ValueError("Interpolation points must contain finite positive frequencies and finite dB")
    if any(source[index][0] >= source[index + 1][0] for index in range(len(source) - 1)):
        raise ValueError("Interpolation frequencies must be strictly increasing")
    result: list[list[float]] = []
    source_index = 0
    for requested in frequencies_hz:
        frequency = float(requested)
        if not math.isfinite(frequency) or frequency <= 0:
            raise ValueError("Requested interpolation frequency must be finite and positive")
        if frequency < source[0][0] or frequency > source[-1][0]:
            continue
        while source_index + 1 < len(source) and source[source_index + 1][0] < frequency:
            source_index += 1
        if source[source_index][0] == frequency:
            value = source[source_index][1]
        elif source_index + 1 < len(source) and source[source_index + 1][0] == frequency:
            value = source[source_index + 1][1]
        else:
            low_frequency, low_db = source[source_index]
            high_frequency, high_db = source[source_index + 1]
            fraction = math.log(frequency / low_frequency) / math.log(high_frequency / low_frequency)
            value = low_db + fraction * (high_db - low_db)
        result.append([frequency, round(value, 6)])
    return result


def _auto_sub_lr24_highpass_attenuation_db(
    frequency_hz: float, crossover_hz: float, sample_rate: float,
) -> float:
    """Return the exact cascaded digital Butterworth-2 high-pass response used by the helper."""
    frequency = float(frequency_hz)
    crossover = float(crossover_hz)
    rate = float(sample_rate)
    if not all(math.isfinite(value) and value > 0 for value in (frequency, crossover, rate)):
        raise ValueError("LR24 response inputs must be finite and positive")
    if frequency >= rate / 2 or crossover >= rate / 2:
        raise ValueError("LR24 response inputs must remain below Nyquist")
    omega_0 = 2.0 * math.pi * crossover / rate
    cos_0, sin_0 = math.cos(omega_0), math.sin(omega_0)
    alpha = sin_0 / (2.0 * math.sqrt(0.5))
    a0 = 1.0 + alpha
    b0 = ((1.0 + cos_0) * 0.5) / a0
    b1 = (-(1.0 + cos_0)) / a0
    b2 = b0
    a1 = (-2.0 * cos_0) / a0
    a2 = (1.0 - alpha) / a0
    omega = 2.0 * math.pi * frequency / rate
    z1 = complex(math.cos(omega), -math.sin(omega))
    z2 = z1 * z1
    one_stage = (b0 + b1 * z1 + b2 * z2) / (1.0 + a1 * z1 + a2 * z2)
    lr24_magnitude = abs(one_stage) ** 2
    return 20.0 * math.log10(max(lr24_magnitude, 1e-300))


def _auto_sub_lr24_frequency_for_attenuation(
    crossover_hz: float, sample_rate: float, max_attenuation_db: float,
) -> float:
    """Find the first frequency above XO whose digital LR24 attenuation passes the threshold."""
    threshold = float(max_attenuation_db)
    if not math.isfinite(threshold) or threshold >= 0:
        raise ValueError("LR24 attenuation threshold must be finite and below 0 dB")
    low = float(crossover_hz)
    high = min(float(sample_rate) * 0.49, max(low * 2.0, low + 1.0))
    while _auto_sub_lr24_highpass_attenuation_db(high, crossover_hz, sample_rate) < threshold:
        next_high = min(float(sample_rate) * 0.49, high * 2.0)
        if next_high <= high:
            raise ValueError("LR24 attenuation threshold is outside usable digital support")
        high = next_high
    for _ in range(64):
        middle = (low + high) * 0.5
        if _auto_sub_lr24_highpass_attenuation_db(middle, crossover_hz, sample_rate) >= threshold:
            high = middle
        else:
            low = middle
    return high


def _analyze_auto_sub_main_target_anchor(
    *, target_curve: dict[str, Any] | None, main_references: dict[str, Any] | None,
    crossover_hz: int, main_highpass_enabled: bool,
) -> dict[str, Any]:
    """Build immutable Main/Target alignment diagnostics; never calculate Gain."""
    diagnostics: dict[str, Any] = {
        "status": "unavailable",
        "reason": None,
        "method": "calibrated Main-only points with log-frequency Target interpolation",
        "gain_calculated": False,
        "crossover_frequency_hz": int(crossover_hz),
        "main_highpass_enabled": bool(main_highpass_enabled),
        "criteria": {
            "lower_bound_rule": "max(common support, exact digital LR24 frequency at -1.0 dB)" if main_highpass_enabled else "max(common support, crossover)",
            "lower_bound_justification": "With Main HP enabled, exclude points where the known helper transfer attenuates Main by more than 1.0 dB; with HP disabled, exclude below-XO bass because this gate is for Main reference above the sub integration boundary.",
            "lr24_transfer": "two cascaded Butterworth-2 high-pass biquads; |H_LR24(e^jw)|=|B(e^jw)/A(e^jw)|^2",
            "maximum_main_hp_attenuation_db": -1.0 if main_highpass_enabled else None,
            "upper_bound_rule": "minimum of actual support and 4x crossover; excludes the measured high-frequency capture floor",
            "minimum_points_per_side": 8,
            "minimum_log_span_octaves": 1.0,
            "support_qc_scope": "structural sampling adequacy only; it does not assert acoustic correctness",
            "target_extrapolation": False,
            "both_sides_required": True,
        },
        "sides": {},
    }
    if not isinstance(target_curve, dict) or len(target_curve.get("points") or []) < 2:
        diagnostics["reason"] = "Target snapshot unavailable"
        return diagnostics
    if not isinstance(main_references, dict) or main_references.get("status") != "completed":
        diagnostics["reason"] = "Completed Main-only L/R snapshots unavailable"
        return diagnostics
    target_points = target_curve.get("points") or []
    try:
        target_min = float(target_points[0][0])
        target_max = float(target_points[-1][0])
        side_points: dict[str, list[list[float]]] = {}
        sample_rates: dict[str, float] = {}
        for side in ("left", "right"):
            reference = main_references.get(side)
            points = reference.get("points") if isinstance(reference, dict) else None
            if not isinstance(reference, dict) or reference.get("status") != "completed":
                raise ValueError(f"Main-only {side} snapshot is not completed")
            if reference.get("exact_sub_mute") is not True:
                raise ValueError(f"Main-only {side} exact-sub-mute confirmation is missing")
            normalized_by_db = float(reference.get("normalized_by_db"))
            if not math.isfinite(normalized_by_db):
                raise ValueError(f"Main-only {side} normalization metadata is invalid")
            if int(reference.get("crossover_frequency_hz")) != int(crossover_hz):
                raise ValueError(f"Main-only {side} crossover does not match the AutoSub job")
            if bool(reference.get("main_highpass_enabled")) != bool(main_highpass_enabled):
                raise ValueError(f"Main-only {side} Main-HP state does not match the AutoSub job")
            sample_rate = float(reference.get("sample_rate"))
            if not math.isfinite(sample_rate) or sample_rate <= 2.0 * float(crossover_hz):
                raise ValueError(f"Main-only {side} sample-rate metadata is invalid")
            sample_rates[side] = sample_rate
            if not isinstance(points, list) or len(points) < 2:
                raise ValueError(f"Main-only {side} calibrated points unavailable")
            parsed = [[float(point[0]), float(point[1])] for point in points]
            if any(not math.isfinite(point[0]) or point[0] <= 0 or not math.isfinite(point[1]) for point in parsed):
                raise ValueError(f"Main-only {side} contains invalid points")
            if any(parsed[index][0] >= parsed[index + 1][0] for index in range(len(parsed) - 1)):
                raise ValueError(f"Main-only {side} frequencies are not strictly increasing")
            side_points[side] = parsed
    except (TypeError, ValueError, IndexError) as exc:
        diagnostics["reason"] = str(exc)
        return diagnostics

    common_low = max(target_min, side_points["left"][0][0], side_points["right"][0][0])
    common_high = min(target_max, side_points["left"][-1][0], side_points["right"][-1][0])
    if sample_rates["left"] != sample_rates["right"]:
        diagnostics["reason"] = "Main-only L/R sample rates do not match"
        return diagnostics
    if main_highpass_enabled:
        transfer_lower = _auto_sub_lr24_frequency_for_attenuation(crossover_hz, sample_rates["left"], -1.0)
        diagnostics["lr24_lower_bound_hz"] = round(transfer_lower, 6)
        diagnostics["lr24_attenuation_at_lower_bound_db"] = round(
            _auto_sub_lr24_highpass_attenuation_db(transfer_lower, crossover_hz, sample_rates["left"]), 6,
        )
        usable_low = max(common_low, transfer_lower)
    else:
        usable_low = max(common_low, float(crossover_hz))
    usable_high = min(common_high, float(crossover_hz) * 4.0)
    diagnostics["common_support_hz"] = [round(common_low, 6), round(common_high, 6)]
    diagnostics["usable_band_hz"] = [round(usable_low, 6), round(usable_high, 6)]
    if usable_high <= usable_low:
        diagnostics["reason"] = "No common Target/Main support remains in the HP/XO-safe anchor band"
        return diagnostics
    span_octaves = math.log2(usable_high / usable_low)
    diagnostics["usable_span_octaves"] = round(span_octaves, 6)
    failures: list[str] = []
    for side in ("left", "right"):
        usable_main = [point for point in side_points[side] if usable_low <= point[0] <= usable_high]
        target_on_main = _auto_sub_log_interpolate_points(target_points, [point[0] for point in usable_main])
        aligned = [
            [main_point[0], main_point[1], target_point[1]]
            for main_point, target_point in zip(usable_main, target_on_main)
        ]
        side_ok = len(aligned) >= 8 and span_octaves >= 1.0
        diagnostics["sides"][side] = {
            "status": "ready" if side_ok else "insufficient_support",
            "point_count": len(aligned),
            "frequency_support_hz": [aligned[0][0], aligned[-1][0]] if aligned else None,
            "aligned_points": aligned,
            "point_format": ["frequency_hz", "calibrated_main_db", "target_db"],
            "source": {
                "sweep_id": main_references[side].get("sweep_id"),
                "measurement_channel": main_references[side].get("measurement_channel"),
                "sample_rate": main_references[side].get("sample_rate"),
                "normalized_by_db": main_references[side].get("normalized_by_db"),
                "exact_sub_mute": True,
            },
        }
        if len(aligned) < 8:
            failures.append(f"{side} has {len(aligned)} usable points; 8 required")
    if span_octaves < 1.0:
        failures.append(f"usable span is {span_octaves:.3f} octaves; 1.0 required")
    if failures:
        diagnostics["reason"] = "; ".join(failures)
        return diagnostics
    anchor_offsets = [
        point[1] - point[2]
        for side in ("left", "right")
        for point in diagnostics["sides"][side]["aligned_points"]
    ]
    diagnostics["target_vertical_offset_db"] = round(statistics.median(anchor_offsets), 6)
    diagnostics["target_anchor_statistic"] = "median(calibrated_main_db - relative_target_db), pooled L/R"
    diagnostics["status"] = "ready"
    diagnostics["reason"] = "Anchor inputs passed"
    diagnostics["target"] = {
        "key": target_curve.get("key"),
        "label": target_curve.get("label"),
        "provenance": target_curve.get("provenance"),
    }
    return json.loads(json.dumps(diagnostics))


def _auto_sub_one_octave_smooth(points: list[list[float]]) -> list[list[float]]:
    """Robust fixed 1/1-octave smoothing using a moving median in log frequency."""
    parsed = [[float(point[0]), float(point[1])] for point in points]
    if len(parsed) < 3 or any(
        not math.isfinite(frequency) or frequency <= 0 or not math.isfinite(db)
        for frequency, db in parsed
    ):
        raise ValueError("Winner curve requires at least three finite points")
    if any(parsed[index][0] >= parsed[index + 1][0] for index in range(len(parsed) - 1)):
        raise ValueError("Winner frequencies must be strictly increasing")
    half_octave = math.sqrt(2.0)
    return [
        [frequency, round(statistics.median(
            value for neighbour_frequency, value in parsed
            if frequency / half_octave <= neighbour_frequency <= frequency * half_octave
        ), 6)]
        for frequency, _value in parsed
    ]


def _auto_sub_third_octave_smooth(points: list[list[float]]) -> list[list[float]]:
    """Robust fixed 1/3-octave smoothing for Stereo corridor checks only."""
    parsed = [[float(point[0]), float(point[1])] for point in points]
    if len(parsed) < 3 or any(
        not math.isfinite(frequency) or frequency <= 0 or not math.isfinite(db)
        for frequency, db in parsed
    ):
        raise ValueError("Corridor curve requires at least three finite points")
    if any(parsed[index][0] >= parsed[index + 1][0] for index in range(len(parsed) - 1)):
        raise ValueError("Corridor frequencies must be strictly increasing")
    half_window = 2.0 ** (1.0 / 6.0)
    return [
        [frequency, round(statistics.median(
            value for neighbour_frequency, value in parsed
            if frequency / half_window <= neighbour_frequency <= frequency * half_window
        ), 6)]
        for frequency, _value in parsed
    ]


def _auto_sub_stereo_corridor_violation(
    *, points: list[list[float]], target_curve: dict[str, Any] | None,
    anchor: dict[str, Any] | None, crossover_hz: int, direction: float,
) -> dict[str, Any]:
    """Measure broad 1/3-octave Target -6/+9 dB violations in one Gain direction."""
    result: dict[str, Any] = {
        "available": False, "relevant": False, "smoothing_octaves": 1.0 / 3.0,
        "corridor_db": [-6.0, 9.0], "direction": "lower" if direction < 0 else "raise",
        "severity_db": 0.0, "reason": None,
    }
    try:
        if not isinstance(anchor, dict) or anchor.get("status") != "ready":
            raise ValueError("Main/Target anchor is unavailable")
        offset = float(anchor.get("target_vertical_offset_db"))
        target_points = (target_curve or {}).get("points")
        if not math.isfinite(offset) or not isinstance(target_points, list) or len(target_points) < 2:
            raise ValueError("Anchored Target is unavailable")
        low, high = max(20.0, 0.5 * float(crossover_hz)), 2.0 * float(crossover_hz)
        smoothed = [point for point in _auto_sub_third_octave_smooth(points) if low <= point[0] <= high]
        target = _auto_sub_log_interpolate_points(target_points, [point[0] for point in smoothed])
        if len(smoothed) < 8 or len(target) != len(smoothed):
            raise ValueError("Stereo corridor has fewer than 8 common points")
        target = _auto_sub_third_octave_smooth(target)
        rows = []
        for measured, target_point in zip(smoothed, target):
            delta = measured[1] - (target_point[1] + offset)
            excess = max(0.0, delta - 9.0) if direction < 0 else max(0.0, -6.0 - delta)
            rows.append((measured[0], excess))
        groups: list[list[tuple[float, float]]] = []
        previous_index = -2
        for index, row in enumerate(rows):
            if row[1] > 0.0:
                if index != previous_index + 1:
                    groups.append([])
                groups[-1].append(row)
                previous_index = index
        relevant_groups = [
            group for group in groups
            if len(group) >= 3 and math.log2(group[-1][0] / group[0][0]) >= (1.0 / 6.0)
        ]
        severity = max((statistics.median(value for _frequency, value in group) for group in relevant_groups), default=0.0)
        result.update({
            "available": True, "relevant": bool(relevant_groups), "severity_db": round(severity, 3),
            "point_count": len(smoothed), "frequency_range_hz": [round(smoothed[0][0], 3), round(smoothed[-1][0], 3)],
            "broad_group_count": len(relevant_groups),
            "reason": "Broad 1/3-octave corridor violation" if relevant_groups else "No broad 1/3-octave corridor violation",
        })
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        result["reason"] = str(exc)
    return result


def _auto_sub_stereo_probe_plan(
    *, correction_plan: dict[str, Any], gain_after: dict[str, Any],
    gain_deltas: dict[str, float], accepted_step1_sides: dict[str, bool],
    after_points: dict[str, list[list[float]]], target_curve: dict[str, Any] | None,
    anchor: dict[str, Any] | None, crossover_hz: int,
) -> dict[str, Any]:
    """Plan one bounded Stereo-only probe when only the >6 dB correction limit failed."""
    result: dict[str, Any] = {"available": False, "deltas_db": {}, "channels": {}, "reason": None}
    if correction_plan.get("reason") != "Measured final Gain correction is implausible":
        result["reason"] = "Stereo probe requires only the >6 dB correction limit to have failed"
        return result
    for side in ("left", "right"):
        channel = ((correction_plan.get("channels") or {}).get(side) or {})
        try:
            response = float(channel.get("response_change_per_db"))
            remaining = float(((gain_after.get("channels") or {}).get(side) or {}).get("target_delta_db"))
            first_step = float(gain_deltas.get(side, 0.0))
            raw_correction = remaining / response
            direction_clear = (
                accepted_step1_sides.get(side) is True
                and 0.2 <= response <= 2.0
                and abs(raw_correction) > 6.0
                and raw_correction * first_step > 0.0
                and remaining * first_step > 0.0
            )
            corridor = _auto_sub_stereo_corridor_violation(
                points=after_points.get(side) or [], target_curve=target_curve, anchor=anchor,
                crossover_hz=crossover_hz, direction=raw_correction,
            )
            eligible = bool(direction_clear and corridor.get("available") and corridor.get("relevant"))
            result["channels"][side] = {
                "eligible": eligible, "response_change_per_db": round(response, 4),
                "remaining_error_db": round(remaining, 3), "raw_correction_db": round(raw_correction, 3),
                "direction_clear": direction_clear, "corridor_before": corridor,
            }
            if eligible:
                result["deltas_db"][side] = -1.0 if raw_correction < 0.0 else 1.0
        except (TypeError, ValueError, ZeroDivisionError):
            result["channels"][side] = {"eligible": False, "reason": "Incomplete Stereo probe inputs"}
    result["available"] = bool(result["deltas_db"])
    result["reason"] = "Bounded Stereo corridor probe planned" if result["available"] else "No Stereo side qualified for a bounded corridor probe"
    return result


def _calculate_auto_sub_gain(
    *, mode: str, target_curve: dict[str, Any] | None, anchor: dict[str, Any] | None,
    winner_curves: dict[str, list[list[float]]], crossover_hz: int,
) -> dict[str, Any]:
    """Calculate bounded diagnostic Gain from calibrated accepted-winner curves."""
    result: dict[str, Any] = {
        "available": False, "gain_calculated": False, "applied": False,
        "method": "fixed 1/1-octave moving-median smoothing of Winner and anchored Target; median Target-minus-Winner deviation",
        "smoothing_octaves": 1.0,
        "bounds_db": [-6.0, 6.0], "reason": None, "channels": {},
    }
    try:
        if not isinstance(anchor, dict) or anchor.get("status") != "ready":
            raise ValueError("Main/Target anchor is unavailable")
        vertical_offset = float(anchor.get("target_vertical_offset_db"))
        if not math.isfinite(vertical_offset):
            raise ValueError("Target vertical offset is invalid")
        target_points = (target_curve or {}).get("points")
        if not isinstance(target_points, list) or len(target_points) < 2:
            raise ValueError("Target snapshot is unavailable")
        requested_low = max(20.0, float(crossover_hz) * 0.5)
        requested_high = float(crossover_hz) * 2.0
        for channel, points in winner_curves.items():
            smoothed = _auto_sub_one_octave_smooth(points)
            usable = [point for point in smoothed if requested_low <= point[0] <= requested_high]
            target_on_winner = _auto_sub_log_interpolate_points(target_points, [point[0] for point in usable])
            if len(target_on_winner) != len(usable) or len(usable) < 8:
                raise ValueError(f"{channel} winner/Target support has fewer than 8 common points")
            smoothed_target = _auto_sub_one_octave_smooth(target_on_winner)
            deviations = [
                (target_point[1] + vertical_offset) - winner_point[1]
                for winner_point, target_point in zip(usable, smoothed_target)
            ]
            target_delta = statistics.median(deviations)
            raw_gain = target_delta
            mad = statistics.median(abs(value - target_delta) for value in deviations)
            bounded = min(6.0, max(-6.0, raw_gain))
            coverage_octaves = math.log2(usable[-1][0] / usable[0][0])
            confidence = "high" if len(usable) >= 24 and coverage_octaves >= 1.5 and mad <= 1.5 else (
                "medium" if len(usable) >= 12 and coverage_octaves >= 1.0 and mad <= 3.0 else "low"
            )
            result["channels"][channel] = {
                "frequency_range_hz": [round(usable[0][0], 3), round(usable[-1][0], 3)],
                "point_count": len(usable), "coverage_octaves": round(coverage_octaves, 3),
                "target_delta_db": round(target_delta, 3),
                "raw_recommendation_db": round(raw_gain, 3), "recommendation_db": round(bounded, 3),
                "clamped": bounded != raw_gain, "median_absolute_deviation_db": round(mad, 3),
                "confidence": confidence,
                "reason": f"{len(usable)} points across {coverage_octaves:.2f} octaves; MAD {mad:.2f} dB",
            }
        if mode in (OUTPUT_MODE_SUBWOOFER_21, OUTPUT_MODE_SUBWOOFER_22):
            channel_values = [entry["raw_recommendation_db"] for entry in result["channels"].values()]
            if not channel_values:
                raise ValueError("No accepted Winner channels are available")
            raw_common = statistics.median(channel_values)
            bounded_common = min(6.0, max(-6.0, raw_common))
            result["recommendation"] = {
                "type": "common", "raw_delta_db": round(raw_common, 3),
                "delta_db": round(bounded_common, 3), "clamped": bounded_common != raw_common,
                "preserves_relative_sub_gain": mode == OUTPUT_MODE_SUBWOOFER_22,
            }
        else:
            result["recommendation"] = {
                "type": "per_channel",
                "left_delta_db": result["channels"]["left"]["recommendation_db"],
                "right_delta_db": result["channels"]["right"]["recommendation_db"],
            }
        confidences = [entry["confidence"] for entry in result["channels"].values()]
        result["confidence"] = "low" if "low" in confidences else ("medium" if "medium" in confidences else "high")
        result["available"] = True
        result["gain_calculated"] = True
        result["reason"] = "Diagnostic recommendation calculated; no audio state changed"
    except (TypeError, ValueError, IndexError, KeyError) as exc:
        result["reason"] = str(exc)
    return json.loads(json.dumps(result))


def _auto_sub_gain_deltas(
    auto_gain: dict[str, Any], mode: str, *, max_abs_db: float = 6.0,
) -> dict[str, float]:
    """Use the residual only as direction/magnitude input for one bounded feedback step."""
    if not isinstance(auto_gain, dict) or not auto_gain.get("gain_calculated"):
        return {}
    limit = abs(float(max_abs_db))
    if not math.isfinite(limit) or limit <= 0:
        return {}
    bounded = lambda value: min(limit, max(-limit, float(value)))
    recommendation = auto_gain.get("recommendation") or {}
    if mode == OUTPUT_MODE_SUBWOOFER_22_STEREO:
        return {
            "left": bounded(recommendation["left_delta_db"]),
            "right": bounded(recommendation["right_delta_db"]),
        }
    delta = bounded(recommendation["delta_db"])
    return {"left": delta, "right": delta}


def _auto_sub_gain_verdict(before: dict[str, Any], after: dict[str, Any], mode: str) -> dict[str, Any]:
    """Accept one Gain attempt unless its residual Target error grows by >0.25 dB."""
    verdict = {"accepted": False, "reason": None, "channels": {}}
    if not before.get("gain_calculated") or not after.get("gain_calculated"):
        verdict["reason"] = "Gain verification inputs unavailable"
        return verdict
    names = ("left", "right")
    accepted = True
    for name in names:
        before_error = abs(float(before["channels"][name]["raw_recommendation_db"]))
        after_error = abs(float(after["channels"][name]["raw_recommendation_db"]))
        channel_ok = after_error <= before_error + 0.25
        accepted = accepted and channel_ok
        verdict["channels"][name] = {
            "before_absolute_residual_db": round(before_error, 3),
            "after_absolute_residual_db": round(after_error, 3),
            "accepted": channel_ok,
        }
    verdict["accepted"] = accepted
    verdict["reason"] = "Gain verification passed" if accepted else "After residual exceeded pre-Gain residual by more than 0.25 dB"
    return verdict


def _auto_sub_gain_response_correction(
    before: dict[str, Any], after: dict[str, Any], applied_step: dict[str, float], mode: str,
) -> dict[str, Any]:
    """Estimate one final correction from the measured broad-band response per applied dB."""
    result: dict[str, Any] = {
        "available": False, "deltas_db": {}, "raw_deltas_db": {}, "applied_deltas_db": {},
        "channels": {}, "reason": None,
    }
    if not before.get("gain_calculated") or not after.get("gain_calculated"):
        result["reason"] = "Gain response inputs unavailable"
        return result
    try:
        for side in ("left", "right"):
            step = float(applied_step[side])
            if abs(step) < 0.05:
                raise ValueError(f"{side} first Gain step is too small to measure sensitivity")
            before_delta = float(before["channels"][side]["target_delta_db"])
            after_delta = float(after["channels"][side]["target_delta_db"])
            response_change = before_delta - after_delta
            sensitivity = response_change / step
            plausible = math.isfinite(sensitivity) and 0.2 <= sensitivity <= 2.0
            result["channels"][side] = {
                "before_target_delta_db": round(before_delta, 3),
                "after_target_delta_db": round(after_delta, 3),
                "applied_step_db": round(step, 3),
                "response_change_db": round(response_change, 3),
                "response_change_per_db": round(sensitivity, 4),
                "plausible": plausible,
            }
            if not plausible:
                raise ValueError(f"{side} measured response per dB is implausible ({sensitivity:.3f})")
        if mode in (OUTPUT_MODE_SUBWOOFER_21, OUTPUT_MODE_SUBWOOFER_22):
            sensitivity = statistics.median(
                result["channels"][side]["response_change_per_db"] for side in ("left", "right")
            )
            remaining = statistics.median(
                float(after["channels"][side]["target_delta_db"]) for side in ("left", "right")
            )
            correction = remaining / sensitivity
            deltas = {"left": correction, "right": correction}
        else:
            deltas = {
                side: float(after["channels"][side]["target_delta_db"])
                / float(result["channels"][side]["response_change_per_db"])
                for side in ("left", "right")
            }
        result["raw_deltas_db"] = {side: round(value, 3) for side, value in deltas.items()}
        if any(not math.isfinite(value) or abs(value) > 6.0 for value in deltas.values()):
            raise ValueError("Measured final Gain correction is implausible")
        applied_deltas = {
            side: max(-6.0 - float(applied_step[side]), min(6.0 - float(applied_step[side]), value))
            for side, value in deltas.items()
        }
        result["applied_deltas_db"] = {side: round(value, 3) for side, value in applied_deltas.items()}
        result["deltas_db"] = dict(result["applied_deltas_db"])
        result["available"] = True
        result["reason"] = "Final correction derived from measured Before/After response"
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        result["reason"] = str(exc)
    return result


def _auto_sub_gain_log_score(diagnostics: dict[str, Any] | None) -> float | None:
    channels = (diagnostics or {}).get("channels") or {}
    values = [abs(float((channels.get(side) or {}).get("target_delta_db"))) for side in ("left", "right")]
    return round(statistics.median(values), 3) if len(values) == 2 and all(math.isfinite(v) for v in values) else None


def _auto_sub_gain_log_line(event: str, payload: dict[str, Any]) -> None:
    logger.info("%s %s", event, json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _auto_sub_22_snapshot_with_gain(
    snapshot: dict[str, Any], *, left_delta_db: float, right_delta_db: float,
) -> dict[str, Any]:
    updated = _auto_sub_snapshot_copy(snapshot)
    subs = updated.setdefault("subwoofers", {})
    for key, delta in (("sub1", left_delta_db), ("sub2", right_delta_db)):
        sub = subs.setdefault(key, {})
        sub["level_db"] = max(-80.0, min(12.0, float(sub.get("level_db", 0.0) or 0.0) + float(delta)))
    return updated


async def _capture_auto_sub_main_references(
    *,
    job: dict[str, Any],
    fc: int,
    input_id: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    auto_sub_sweep_profile: dict[str, Any],
    auto_sub_rate: int,
    output_mode: str,
    original_config_snapshot: dict[str, Any],
) -> None:
    """Capture exactly one L and one R Main-only reference before candidate scans."""
    subwoofer = original_config_snapshot.get("subwoofer") if isinstance(original_config_snapshot.get("subwoofer"), dict) else {}
    sub1 = _auto_sub_22_sub(original_config_snapshot, "sub1")
    sub2 = _auto_sub_22_sub(original_config_snapshot, "sub2")
    is_22 = output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES
    main_highpass_enabled = bool(
        _auto_sub_22_global_config(original_config_snapshot).get("main_highpass_enabled", True)
        if is_22 else subwoofer.get("main_highpass_enabled", True)
    )
    left_delay = float(sub1.get("alignment_ms", 0.0) or 0.0) if is_22 else float(subwoofer.get("sub_alignment_ms", 0.0) or 0.0)
    right_delay = float(sub2.get("alignment_ms", 0.0) or 0.0) if is_22 else left_delay
    job["stage"] = "main_reference"
    job["main_references"] = {
        "status": "running",
        "exact_sub_mute": True,
        "crossover_frequency_hz": int(fc),
        "main_highpass_enabled": main_highpass_enabled,
        "left": {"status": "pending"},
        "right": {"status": "pending"},
    }
    results: dict[str, dict[str, Any]] = {}
    for index, side in enumerate(("left", "right"), start=1):
        if _auto_sub_cancel_requested(job):
            job["main_references"][side] = {"status": "cancelled"}
            break
        delay = left_delay if side == "left" else right_delay
        result = await _measure_auto_sub_candidate(
            delay_ms=delay,
            job=job,
            candidate_index=index,
            total=2,
            stage="main_reference",
            fc=fc,
            input_id=input_id,
            channel=side,
            mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref,
            calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes,
            auto_sub_sweep_profile=auto_sub_sweep_profile,
            auto_sub_rate=auto_sub_rate,
            original_level=float(subwoofer.get("sub_level_db", 0.0) or 0.0),
            original_polarity=str(subwoofer.get("sub_polarity") or "normal"),
            original_highpass=main_highpass_enabled,
            measurement_label=f"Main-only reference: {side.title()} ({index}/2)",
            measure_channel=side,
            output_mode=output_mode,
            original_config_snapshot=original_config_snapshot,
            sub1_alignment_ms=left_delay,
            sub2_alignment_ms=right_delay,
            active_subs=("sub1", "sub2"),
            exact_sub_mute=True,
        )
        results[side] = result
        job["main_references"][side] = {
            "status": result.get("status"),
            "points": json.loads(json.dumps(result.get("calibrated_points"))) if result.get("calibrated_points") else [],
            "normalized_by_db": result.get("normalized_by_db"),
            "sweep_id": result.get("sweep_id"),
            "channel": side,
            "measurement_channel": result.get("measurement_channel"),
            "sample_rate": result.get("sample_rate"),
            "crossover_frequency_hz": int(fc),
            "main_highpass_enabled": main_highpass_enabled,
            "exact_sub_mute": bool(result.get("exact_sub_mute")),
        }
        if result.get("error"):
            job["main_references"][side]["error"] = str(result.get("error"))
    complete = len(results) == 2 and all(
        result.get("status") == "completed" and len(result.get("calibrated_points") or []) >= 2
        for result in results.values()
    )
    job["main_references"]["status"] = "completed" if complete else "unavailable"
    if not complete:
        failures = [f"{side}: {result.get('status')} ({result.get('error') or 'no calibrated points'})" for side, result in results.items() if result.get("status") != "completed" or not result.get("calibrated_points")]
        if len(results) < 2:
            failures.append("reference capture cancelled before both sides completed")
        job["auto_gain"] = {"available": False, "reason": "Main-only reference unavailable: " + "; ".join(failures)}
    job["main_target_anchor"] = _analyze_auto_sub_main_target_anchor(
        target_curve=job.get("target_curve"),
        main_references=job.get("main_references"),
        crossover_hz=fc,
        main_highpass_enabled=main_highpass_enabled,
    )
    if job["main_target_anchor"].get("status") == "ready":
        job["auto_gain"] = {
            "available": False,
            "reason": "Main/Target anchor ready; Gain calculation is not implemented",
        }
    elif complete:
        job["auto_gain"] = {
            "available": False,
            "reason": "Main/Target anchor unavailable: " + str(job["main_target_anchor"].get("reason") or "unknown reason"),
        }
async def _measure_auto_sub_combined_candidate(
    *,
    delay_ms: float,
    job: dict[str, Any],
    candidate_index: int,
    total: int,
    sweep_index_start: int,
    sweep_total: int,
    stage: str,
    fc: int,
    input_id: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    auto_sub_sweep_profile: dict[str, Any],
    auto_sub_rate: int,
    original_level: float,
    original_polarity: str,
    original_highpass: bool,
    output_mode: str = OUTPUT_MODE_SUBWOOFER_21,
    original_config_snapshot: dict[str, Any] | None = None,
    sub1_alignment_ms: float | None = None,
    sub2_alignment_ms: float | None = None,
    active_subs: tuple[str, ...] = ("sub1",),
    sub1_polarity: str | None = None,
    sub2_polarity: str | None = None,
) -> dict[str, Any]:
    """Measure both L and R for one AutoSub delay candidate."""
    _combined_start = time.monotonic()

    def _last_sweep_timing(channel_name: str) -> dict[str, Any] | None:
        for timing in reversed(job.get("_sweep_timings", [])):
            if (
                timing.get("channel") == channel_name
                and timing.get("stage") == stage
                and round(float(timing.get("delay_ms", -9999)), 2) == round(float(delay_ms), 2)
            ):
                return timing
        return None

    def _append_combined_timing(status: str, left_result: dict[str, Any] | None = None, right_result: dict[str, Any] | None = None) -> None:
        left_timing = _last_sweep_timing("left")
        right_timing = _last_sweep_timing("right")
        job.setdefault("_combined_candidate_timings", []).append({
            "delay_ms": delay_ms,
            "stage": stage,
            "candidate": candidate_index,
            "status": status,
            "left_status": (left_result or {}).get("status"),
            "right_status": (right_result or {}).get("status"),
            "left_total_ms": ((left_timing or {}).get("durations", {}) or {}).get("total_ms"),
            "right_total_ms": ((right_timing or {}).get("durations", {}) or {}).get("total_ms"),
            "total_ms": round((time.monotonic() - _combined_start) * 1000, 1),
        })

    if _auto_sub_cancel_requested(job):
        _append_combined_timing("cancelled")
        return _auto_sub_cancelled_candidate(delay_ms, stage)

    if stage == "sub1_coarse":
        label = "Optimizing Sub 1"
    elif stage == "sub2_coarse":
        label = "Optimizing Sub 2"
    elif stage == "combined_matrix":
        label = "Combined Matrix"
    else:
        label = "Fine-Scan" if stage == "fine" else "Coarse scan"
    pair_suffix = ""
    if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
        s1 = _auto_sub_clamped_delay(sub1_alignment_ms if sub1_alignment_ms is not None else delay_ms)
        s2 = _auto_sub_clamped_delay(sub2_alignment_ms if sub2_alignment_ms is not None else 0.0)
        pair_suffix = f" (S1 {s1:.2f} ms / S2 {s2:.2f} ms)"
    left_result = await _measure_auto_sub_candidate(
        delay_ms=delay_ms,
        job=job,
        candidate_index=sweep_index_start,
        total=sweep_total,
        stage=stage,
        fc=fc,
        input_id=input_id,
        channel="left",
        mic_input_channel=mic_input_channel,
        reference_input_channel=reference_input_channel,
        calibration_ref=calibration_ref,
        calibration_filename=calibration_filename,
        calibration_bytes=calibration_bytes,
        auto_sub_sweep_profile=auto_sub_sweep_profile,
        auto_sub_rate=auto_sub_rate,
        original_level=original_level,
        original_polarity=original_polarity,
        original_highpass=original_highpass,
        measurement_label=f"{label}: L meas {candidate_index}/{total} @ {delay_ms:.2f} ms{pair_suffix}",
        candidate_current=candidate_index,
        candidate_total=total,
        measure_channel="left",
        output_mode=output_mode,
        original_config_snapshot=original_config_snapshot,
        sub1_alignment_ms=sub1_alignment_ms,
        sub2_alignment_ms=sub2_alignment_ms,
        active_subs=active_subs,
        sub1_polarity=sub1_polarity,
        sub2_polarity=sub2_polarity,
    )
    if _auto_sub_cancel_requested(job):
        _append_combined_timing("cancelled", left_result=left_result)
        return _auto_sub_cancelled_candidate(delay_ms, stage)

    right_result = await _measure_auto_sub_candidate(
        delay_ms=delay_ms,
        job=job,
        candidate_index=sweep_index_start + 1,
        total=sweep_total,
        stage=stage,
        fc=fc,
        input_id=input_id,
        channel="right",
        mic_input_channel=mic_input_channel,
        reference_input_channel=reference_input_channel,
        calibration_ref=calibration_ref,
        calibration_filename=calibration_filename,
        calibration_bytes=calibration_bytes,
        auto_sub_sweep_profile=auto_sub_sweep_profile,
        auto_sub_rate=auto_sub_rate,
        original_level=original_level,
        original_polarity=original_polarity,
        original_highpass=original_highpass,
        measurement_label=f"{label}: R meas {candidate_index}/{total} @ {delay_ms:.2f} ms{pair_suffix}",
        candidate_current=candidate_index,
        candidate_total=total,
        measure_channel="right",
        output_mode=output_mode,
        original_config_snapshot=original_config_snapshot,
        sub1_alignment_ms=sub1_alignment_ms,
        sub2_alignment_ms=sub2_alignment_ms,
        active_subs=active_subs,
        sub1_polarity=sub1_polarity,
        sub2_polarity=sub2_polarity,
    )
    if _auto_sub_cancel_requested(job):
        _append_combined_timing("cancelled", left_result=left_result, right_result=right_result)
        return _auto_sub_cancelled_candidate(delay_ms, stage)

    left_points = left_result.get("points") or []
    right_points = right_result.get("points") or []
    points = left_points if len(left_points) >= 3 else right_points
    status = "completed" if (len(left_points) >= 3 or len(right_points) >= 3) else "failed"
    _append_combined_timing(status, left_result=left_result, right_result=right_result)

    candidate = {
        "delay_ms": delay_ms,
        "name": str(delay_ms),
        "points": points,
        "points_left": left_points,
        "points_right": right_points,
        "calibrated_points_left": left_result.get("calibrated_points") or [],
        "calibrated_points_right": right_result.get("calibrated_points") or [],
        "normalized_by_db_left": left_result.get("normalized_by_db"),
        "normalized_by_db_right": right_result.get("normalized_by_db"),
        "sweep_id": left_result.get("sweep_id", ""),
        "sweep_id_left": left_result.get("sweep_id", ""),
        "sweep_id_right": right_result.get("sweep_id", ""),
        "status": status,
        "scan": stage,
        "status_left": left_result.get("status"),
        "status_right": right_result.get("status"),
        "stage_output_peaks": {
            "left": left_result.get("stage_output_peaks"),
            "right": right_result.get("stage_output_peaks"),
        },
        "combined_candidate": True,
    }
    if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
        sub1_delay = _auto_sub_clamped_delay(sub1_alignment_ms if sub1_alignment_ms is not None else delay_ms)
        sub2_delay = _auto_sub_clamped_delay(sub2_alignment_ms if sub2_alignment_ms is not None else 0.0)
        candidate.update({
            "sub1_alignment_ms": sub1_delay,
            "sub2_alignment_ms": sub2_delay,
            "name": _auto_sub_22_name(sub1_delay, sub2_delay),
            "active_subs": list(active_subs),
            "sub1_polarity": sub1_polarity,
            "sub2_polarity": sub2_polarity,
        })
    return candidate



def _finalize_autosub_job(job: dict[str, Any] | None, job_id: str) -> None:
    """Transition an AutoSub job to cancelled and log cleanup.

    Cancellation semantics:

    - The cancel endpoint sets cancel_requested only on non-terminal jobs,
      so status="completed" together with cancel_requested=True means the
      cancel request arrived BEFORE the worker committed completion; such a
      job is finalized as cancelled.
    - A genuine worker failure is never relabelled cancelled by the
      finalizer, even when a cancel was requested concurrently.
    - A job already final as cancelled stays cancelled; a job that
      completed without a pending cancel request stays completed.
    """
    if job is None:
        logger.warning("AUTOSUB job=%s worker finished (job missing)", job_id)
        return
    status = str(job.get("status") or "").lower()
    cancel_requested = bool(job.get("cancel_requested"))
    if status in {"failed", "cancelled"}:
        pass
    elif status == "cancelling" or cancel_requested:
        job["status"] = "cancelled"
        job["message"] = "Auto Sub Optimize cancelled."
        job["cancel_requested"] = True
        job["result"] = None
        job["error"] = None
        logger.info(
            "AUTOSUB job=%s worker finished (cancel committed; prior status=%s)",
            job_id, status,
        )
    if isinstance(job.get("result"), dict):
        job["result"]["target_curve"] = json.loads(json.dumps(job.get("target_curve"))) if job.get("target_curve") else None
        job["result"]["auto_gain"] = json.loads(json.dumps(job.get("auto_gain")))
        job["result"]["main_references"] = json.loads(json.dumps(job.get("main_references"))) if job.get("main_references") else None
        job["result"]["main_target_anchor"] = json.loads(json.dumps(job.get("main_target_anchor"))) if job.get("main_target_anchor") else None
        job["result"]["polarity_check"] = json.loads(json.dumps(job.get("polarity_check"))) if job.get("polarity_check") else None
    logger.info("AUTOSUB job=%s cleanup complete state=%s", job_id, job.get("status") or "idle")


async def _finish_auto_sub_worker(job: dict[str, Any] | None, job_id: str) -> None:
    """Release the shared AutoSub lock, finalize the job and schedule its cleanup task.

    Ownership structure: the lock is released unconditionally in the outer
    finally.  Neither a failing sample-rate session unregister nor a failing
    finalize may prevent the release, otherwise every subsequent AutoSub
    operation would block forever with 423.  Cleanup failures are logged and
    never overwrite the worker/job outcome (a failed job stays failed, a
    cancelled job stays cancelled).
    """
    from main import measurement_sr_session
    try:
        if measurement_sr_session is not None:
            try:
                await measurement_sr_session.unregister_auto_sub(job_id)
            except Exception:
                logger.exception(
                    "AUTOSUB job=%s measurement sample-rate session unregister failed", job_id
                )
        try:
            _finalize_autosub_job(job, job_id)
        except Exception:
            logger.exception("AUTOSUB job=%s finalize failed", job_id)
    finally:
        try:
            _auto_sub_lock.release()
        except RuntimeError:
            # The lock is not held (already released); the job status is final.
            logger.debug("AUTOSUB job=%s lock release skipped (not held)", job_id)

    async def _cleanup_autosub_job():
        await asyncio.sleep(600)
        _AUTO_SUB_JOBS.pop(job_id, None)

    cleanup_task = asyncio.create_task(_cleanup_autosub_job())
    _AUTO_SUB_CLEANUP_TASKS.add(cleanup_task)
    cleanup_task.add_done_callback(_AUTO_SUB_CLEANUP_TASKS.discard)


async def _run_auto_sub_22_optimize(
    job_id: str,
    input_id: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    sub1_scan_delays: list[float],
    sub2_scan_delays: list[float],
    fc: int,
    original_config_snapshot: dict[str, Any],
    fine_step_ms: float,
    entry_epoch: int | None = None,
) -> None:
    from main import (
        measurement_sr_session,
        measurement_store,
        subwoofer_runtime,
    )
    from measurement_session import (
        MeasurementEntryInvalidated,
        _resolve_measurement_start_sample_rate,
    )
    global _auto_sub_lock
    from samplerate import _load_audio_output_mode, set_audio_output_mode

    job = _AUTO_SUB_JOBS.get(job_id)
    if not job:
        _auto_sub_lock.release()
        return

    async def _restore_original_config() -> None:
        await _restore_auto_sub_original_config(original_config_snapshot)

    original_sub1 = _auto_sub_22_sub(original_config_snapshot, "sub1")
    original_sub2 = _auto_sub_22_sub(original_config_snapshot, "sub2")
    original_sub1_alignment = float(original_sub1.get("alignment_ms", 0.0) or 0.0)
    original_sub2_alignment = float(original_sub2.get("alignment_ms", 0.0) or 0.0)

    def _matrix_delays(center: float) -> list[float]:
        return [_auto_sub_clamped_delay(center + offset) for offset in (-fine_step_ms, 0.0, fine_step_ms)]

    def _valid_lr(result: dict[str, Any]) -> bool:
        return _auto_sub_has_points(result, "points_left") or _auto_sub_has_points(result, "points_right")

    def _same_pair(pair: tuple[float, float], sub1_alignment: float, sub2_alignment: float) -> bool:
        return abs(pair[0] - sub1_alignment) <= 0.05 and abs(pair[1] - sub2_alignment) <= 0.05

    try:
        if measurement_sr_session is not None:
            try:
                await measurement_sr_session.register_auto_sub(job_id, entry_epoch=entry_epoch)
            except MeasurementEntryInvalidated:
                logger.info(
                    "AUTOSUB job=%s entry invalidated by measurement window close",
                    job_id,
                )
                job["status"] = "cancelled"
                job["message"] = "Auto Sub Optimize cancelled because the measurement window was closed."
                return
        if _auto_sub_cancel_requested(job):
            logger.info("AUTOSUB job=%s cancel observed (before sweeps)", job_id)
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        auto_sub_sweep_profile = _auto_sub_sweep_profile(fc)
        auto_sub_rate = _resolve_measurement_start_sample_rate()
        await _capture_auto_sub_main_references(
            job=job, fc=fc, input_id=input_id,
            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
            auto_sub_rate=auto_sub_rate, output_mode=OUTPUT_MODE_SUBWOOFER_22,
            original_config_snapshot=original_config_snapshot,
        )
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        coarse1_results: list[dict[str, Any]] = []
        coarse2_results: list[dict[str, Any]] = []
        matrix_results: list[dict[str, Any]] = []
        sub1_sweep_total = len(sub1_scan_delays) * 2
        sub2_sweep_total = len(sub2_scan_delays) * 2
        matrix_sweep_start = sub1_sweep_total + sub2_sweep_total
        matrix_sweep_total = matrix_sweep_start + 18

        job["stage"] = "sub1_coarse"
        for idx, delay_ms in enumerate(sub1_scan_delays):
            coarse1_results.append(await _measure_auto_sub_combined_candidate(
                delay_ms=delay_ms,
                job=job,
                candidate_index=idx + 1,
                total=len(sub1_scan_delays),
                sweep_index_start=(idx * 2) + 1,
                sweep_total=matrix_sweep_total,
                stage="sub1_coarse",
                fc=fc,
                input_id=input_id,
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate,
                original_level=0.0,
                original_polarity="normal",
                original_highpass=True,
                output_mode=OUTPUT_MODE_SUBWOOFER_22,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=delay_ms,
                sub2_alignment_ms=original_sub2_alignment,
                active_subs=("sub1",),
            ))
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        coarse1_valid = [result for result in coarse1_results if _valid_lr(result)]
        if not coarse1_valid:
            job["status"] = "failed"
            job["message"] = "No valid Sub 1 coarse sweep results to score"
            job["error"] = {"detail": "Sub 1 coarse sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return
        sub1_scoring = _score_auto_sub_combined_candidates(
            coarse1_results,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_sub1_alignment,
        )
        sub1_winner = sub1_scoring["winner"]
        sub1_winner_delay = _auto_sub_clamped_delay(float(sub1_winner.get("delay_ms", original_sub1_alignment) or original_sub1_alignment))

        job["stage"] = "sub2_coarse"
        for idx, delay_ms in enumerate(sub2_scan_delays):
            coarse2_results.append(await _measure_auto_sub_combined_candidate(
                delay_ms=delay_ms,
                job=job,
                candidate_index=idx + 1,
                total=len(sub2_scan_delays),
                sweep_index_start=sub1_sweep_total + (idx * 2) + 1,
                sweep_total=matrix_sweep_total,
                stage="sub2_coarse",
                fc=fc,
                input_id=input_id,
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate,
                original_level=0.0,
                original_polarity="normal",
                original_highpass=True,
                output_mode=OUTPUT_MODE_SUBWOOFER_22,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=original_sub1_alignment,
                sub2_alignment_ms=delay_ms,
                active_subs=("sub2",),
            ))
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        coarse2_valid = [result for result in coarse2_results if _valid_lr(result)]
        if not coarse2_valid:
            job["status"] = "failed"
            job["message"] = "No valid Sub 2 coarse sweep results to score"
            job["error"] = {"detail": "Sub 2 coarse sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return
        sub2_scoring = _score_auto_sub_combined_candidates(
            coarse2_results,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_sub2_alignment,
        )
        sub2_winner = sub2_scoring["winner"]
        sub2_winner_delay = _auto_sub_clamped_delay(float(sub2_winner.get("delay_ms", original_sub2_alignment) or original_sub2_alignment))

        sub1_matrix = _matrix_delays(sub1_winner_delay)
        sub2_matrix = _matrix_delays(sub2_winner_delay)
        matrix_pairs = [(sub1_delay, sub2_delay) for sub1_delay in sub1_matrix for sub2_delay in sub2_matrix]
        incumbent_pair = (
            _auto_sub_clamped_delay(original_sub1_alignment),
            _auto_sub_clamped_delay(original_sub2_alignment),
        )
        incumbent_in_matrix = any(_same_pair(pair, incumbent_pair[0], incumbent_pair[1]) for pair in matrix_pairs)
        if not incumbent_in_matrix:
            matrix_pairs.append(incumbent_pair)
        job["combined_matrix"] = {
            "status": "running",
            "fine_step_ms": fine_step_ms,
            "sub1_candidates": sub1_matrix,
            "sub2_candidates": sub2_matrix,
            "incumbent_pair": {"sub1_alignment_ms": incumbent_pair[0], "sub2_alignment_ms": incumbent_pair[1]},
            "incumbent_in_matrix": incumbent_in_matrix,
            "candidates": [
                {
                    "sub1_alignment_ms": a,
                    "sub2_alignment_ms": b,
                    "incumbent_pair": _same_pair((a, b), incumbent_pair[0], incumbent_pair[1]),
                }
                for a, b in matrix_pairs
            ],
        }
        matrix_sweep_total = matrix_sweep_start + (len(matrix_pairs) * 2)

        job["stage"] = "combined_matrix"
        for idx, (sub1_delay, sub2_delay) in enumerate(matrix_pairs):
            matrix_results.append(await _measure_auto_sub_combined_candidate(
                delay_ms=sub1_delay,
                job=job,
                candidate_index=idx + 1,
                total=len(matrix_pairs),
                sweep_index_start=matrix_sweep_start + (idx * 2) + 1,
                sweep_total=matrix_sweep_total,
                stage="combined_matrix",
                fc=fc,
                input_id=input_id,
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate,
                original_level=0.0,
                original_polarity="normal",
                original_highpass=True,
                output_mode=OUTPUT_MODE_SUBWOOFER_22,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=sub1_delay,
                sub2_alignment_ms=sub2_delay,
                active_subs=("sub1", "sub2"),
            ))
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        matrix_valid = [result for result in matrix_results if _valid_lr(result)]
        if not matrix_valid:
            job["status"] = "failed"
            job["message"] = "No valid Combined Matrix sweep results to score"
            job["error"] = {"detail": "Combined Matrix sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return

        matrix_scoring = _score_auto_sub_matrix_candidates(
            matrix_results,
            crossover_hz=fc,
            original_sub1_alignment_ms=original_sub1_alignment,
            original_sub2_alignment_ms=original_sub2_alignment,
        )
        winner = matrix_scoring["accepted_winner"]
        gain_winner = next(
            (candidate for candidate in matrix_results
             if round(float(candidate.get("sub1_alignment_ms", 0.0)), 2) == round(float(winner.get("sub1_alignment_ms", 0.0)), 2)
             and round(float(candidate.get("sub2_alignment_ms", 0.0)), 2) == round(float(winner.get("sub2_alignment_ms", 0.0)), 2)),
            {},
        )
        best_sub1 = _auto_sub_clamped_delay(float(winner.get("sub1_alignment_ms", sub1_winner_delay) or sub1_winner_delay))
        best_sub2 = _auto_sub_clamped_delay(float(winner.get("sub2_alignment_ms", sub2_winner_delay) or sub2_winner_delay))
        incumbent_polarities = (str(original_sub1.get("polarity", "normal")), str(original_sub2.get("polarity", "normal")))
        selected_polarities = incumbent_polarities
        polarity_candidates: list[dict[str, Any]] = [dict(gain_winner, delay_ms=0.0)]
        alternative_polarities = [
            (_auto_sub_opposite_polarity(incumbent_polarities[0]), incumbent_polarities[1]),
            (incumbent_polarities[0], _auto_sub_opposite_polarity(incumbent_polarities[1])),
            (_auto_sub_opposite_polarity(incumbent_polarities[0]), _auto_sub_opposite_polarity(incumbent_polarities[1])),
        ]
        for idx, polarities in enumerate(alternative_polarities, 1):
            measured = await _measure_auto_sub_combined_candidate(
                delay_ms=best_sub1, job=job, candidate_index=idx, total=3,
                sweep_index_start=matrix_sweep_total + (idx - 1) * 2 + 1,
                sweep_total=matrix_sweep_total + 6, stage="polarity_check", fc=fc,
                input_id=input_id, mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                original_level=0.0, original_polarity="normal", original_highpass=True,
                output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                active_subs=("sub1", "sub2"), sub1_polarity=polarities[0], sub2_polarity=polarities[1],
            )
            polarity_candidates.append(dict(measured, delay_ms=float(idx), tested_polarities=polarities))
        polarity_scoring = _score_auto_sub_combined_candidates(polarity_candidates, crossover_hz=fc, low_guard_reference_delay_ms=0.0)
        polarity_winner = polarity_scoring["winner"]
        incumbent_scored = _auto_sub_result_for_delay(polarity_scoring["results"], 0.0) or {}
        alternative_scored = polarity_winner if float(polarity_winner.get("delay_ms", 0.0)) != 0.0 else {}
        polarity_decision = _auto_sub_polarity_decision(incumbent_scored, alternative_scored) if alternative_scored else {
            "accepted": False, "reason": "incumbent_best", "score_gain": 0.0, "min_score_gain": 0.03,
        }
        if polarity_decision["accepted"]:
            selected_idx = int(round(float(alternative_scored["delay_ms"])))
            selected_polarities = alternative_polarities[selected_idx - 1]
            selected_measurement = polarity_candidates[selected_idx]
            refinement: list[dict[str, Any]] = []
            refinements = [(a, b) for a in _matrix_delays(best_sub1) for b in _matrix_delays(best_sub2)]
            for idx, (delay1, delay2) in enumerate(refinements):
                refinement.append(await _measure_auto_sub_combined_candidate(
                    delay_ms=delay1, job=job, candidate_index=idx + 1, total=9,
                    sweep_index_start=matrix_sweep_total + 7 + idx * 2,
                    sweep_total=matrix_sweep_total + 24, stage="polarity_refine", fc=fc,
                    input_id=input_id, mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                    original_level=0.0, original_polarity="normal", original_highpass=True,
                    output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=original_config_snapshot,
                    sub1_alignment_ms=delay1, sub2_alignment_ms=delay2, active_subs=("sub1", "sub2"),
                    sub1_polarity=selected_polarities[0], sub2_polarity=selected_polarities[1],
                ))
            refined_scoring = _score_auto_sub_matrix_candidates(refinement, crossover_hz=fc)
            refined_winner = refined_scoring["winner"]
            best_sub1 = float(refined_winner["sub1_alignment_ms"])
            best_sub2 = float(refined_winner["sub2_alignment_ms"])
            gain_winner = next((row for row in refinement if abs(float(row.get("sub1_alignment_ms", 0))-best_sub1)<0.01 and abs(float(row.get("sub2_alignment_ms", 0))-best_sub2)<0.01), selected_measurement)
            polarity_decision["refinement"] = {"winner": refined_winner, "candidate_count": 9}
        polarity_snapshot = _auto_sub_snapshot_copy(original_config_snapshot)
        polarity_snapshot.setdefault("subwoofers", {}).setdefault("sub1", {})["polarity"] = selected_polarities[0]
        polarity_snapshot.setdefault("subwoofers", {}).setdefault("sub2", {})["polarity"] = selected_polarities[1]
        job["polarity_check"] = {**polarity_decision, "incumbent": incumbent_polarities, "selected": selected_polarities, "alternatives_tested": alternative_polarities}
        job["auto_gain"] = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_22,
            target_curve=job.get("target_curve"), anchor=job.get("main_target_anchor"),
            winner_curves={
                "left": gain_winner.get("calibrated_points_left") or [],
                "right": gain_winner.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        logger.info("AUTOSUB_GAIN mode=2.2_mono diagnostics=%s", json.dumps(job["auto_gain"], sort_keys=True))
        gain_deltas = _auto_sub_gain_deltas(job["auto_gain"], OUTPUT_MODE_SUBWOOFER_22, max_abs_db=6.0)
        _auto_sub_gain_log_line("AUTOGAIN_INIT", {
            "mode": OUTPUT_MODE_SUBWOOFER_22, "xo_hz": fc,
            "target": (job.get("target_curve") or {}).get("label"),
            "anchor_hz": (job.get("main_target_anchor") or {}).get("usable_band_hz"),
            "target_offset_db": (job.get("main_target_anchor") or {}).get("target_vertical_offset_db"),
            "gain_before": {"sub1": float(original_sub1.get("level_db", 0.0)), "sub2": float(original_sub2.get("level_db", 0.0))},
            "winner_delta_left": (job["auto_gain"].get("channels", {}).get("left") or {}).get("target_delta_db"),
            "winner_delta_right": (job["auto_gain"].get("channels", {}).get("right") or {}).get("target_delta_db"),
            "combined_delta_db": (job["auto_gain"].get("recommendation") or {}).get("raw_delta_db"),
            "first_step_db": gain_deltas.get("left"),
        })
        gain_snapshot = _auto_sub_22_snapshot_with_gain(
            polarity_snapshot,
            left_delta_db=gain_deltas.get("left", 0.0), right_delta_db=gain_deltas.get("right", 0.0),
        )
        candidate_ledger = (
            _auto_sub_candidate_ledger(
                coarse1_results, sub1_scoring, mode="2.2_mono", phase="sub1_coarse",
                roles={"coarse_winner": sub1_winner},
            )
            + _auto_sub_candidate_ledger(
                coarse2_results, sub2_scoring, mode="2.2_mono", phase="sub2_coarse",
                roles={"coarse_winner": sub2_winner},
            )
            + _auto_sub_candidate_ledger(
                matrix_results, matrix_scoring, mode="2.2_mono", phase="matrix",
                roles={
                    "matrix_winner": matrix_scoring.get("matrix_winner"),
                    "final_accepted_winner": winner,
                },
                requested_incumbent={
                    "sub1_alignment_ms": original_sub1_alignment,
                    "sub2_alignment_ms": original_sub2_alignment,
                },
            )
        )

        apply_ok = False
        try:
            sub_config = _auto_sub_22_global_config(gain_snapshot)
            subwoofers_config = _auto_sub_22_candidate_subwoofers(
                gain_snapshot,
                sub1_alignment_ms=best_sub1,
                sub2_alignment_ms=best_sub2,
                active_subs=("sub1", "sub2"),
            )
            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_22, sub_config, subwoofers_config)
            if subwoofer_runtime is not None:
                config = SubwooferRuntimeConfig.from_overview(get_audio_output_overview())
                await subwoofer_runtime.sync(config)
            await asyncio.sleep(0.3)
            verify = _load_audio_output_mode()
            apply_ok = _auto_sub_22_verify_alignment(verify, best_sub1, best_sub2)
        except Exception:
            logger.exception("Auto-sub 2.2: failed to apply winner pair %.2f / %.2f ms", best_sub1, best_sub2)

        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        if not apply_ok:
            job["status"] = "failed"
            job["message"] = f"Scoring succeeded but failed to apply winner pair {best_sub1:.2f} / {best_sub2:.2f} ms"
            job["error"] = {"detail": "Winner apply failed - original config restored"}
            await _restore_original_config()
            return

        gain_after_sweep = await _measure_auto_sub_combined_candidate(
            delay_ms=best_sub1, job=job, candidate_index=1, total=1,
            sweep_index_start=matrix_sweep_total + 1, sweep_total=matrix_sweep_total + 2,
            stage="gain_after", fc=fc, input_id=input_id,
            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
            auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
            original_highpass=bool(_auto_sub_22_global_config(gain_snapshot).get("main_highpass_enabled", True)),
            output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=gain_snapshot,
            sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2, active_subs=("sub1", "sub2"),
        )
        gain_after = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_22, target_curve=job.get("target_curve"),
            anchor=job.get("main_target_anchor"), winner_curves={
                "left": gain_after_sweep.get("calibrated_points_left") or [],
                "right": gain_after_sweep.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        gain_verdict = _auto_sub_gain_verdict(job["auto_gain"], gain_after, OUTPUT_MODE_SUBWOOFER_22)
        final_gain_deltas = gain_deltas if gain_verdict["accepted"] else {"left": 0.0, "right": 0.0}
        final_gain_snapshot = gain_snapshot if gain_verdict["accepted"] else polarity_snapshot
        final_gain_sweep = gain_after_sweep if gain_verdict["accepted"] else gain_winner
        correction_deltas: dict[str, float] = {}
        correction_plan = None
        correction_after = None
        correction_verdict = None
        if not gain_verdict["accepted"]:
            rollback_subs = _auto_sub_22_candidate_subwoofers(
                polarity_snapshot, sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                active_subs=("sub1", "sub2"),
            )
            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_22, _auto_sub_22_global_config(polarity_snapshot), rollback_subs)
            if subwoofer_runtime is not None:
                await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        else:
            correction_plan = _auto_sub_gain_response_correction(
                job["auto_gain"], gain_after, gain_deltas, OUTPUT_MODE_SUBWOOFER_22,
            )
            correction_deltas = correction_plan.get("deltas_db") or {}
            correction_delta = correction_deltas.get("left", 0.0)
            if not correction_plan.get("available"):
                correction_verdict = {
                    "accepted": False,
                    "reason": correction_plan.get("reason"),
                    "channels": {},
                    "step1_retained": True,
                }
            elif abs(correction_delta) > 0.0005:
                correction_snapshot = _auto_sub_22_snapshot_with_gain(
                    gain_snapshot, left_delta_db=correction_delta, right_delta_db=correction_delta,
                )
                set_audio_output_mode(
                    OUTPUT_MODE_SUBWOOFER_22, _auto_sub_22_global_config(correction_snapshot),
                    _auto_sub_22_candidate_subwoofers(
                        correction_snapshot, sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                        active_subs=("sub1", "sub2"),
                    ),
                )
                if subwoofer_runtime is not None:
                    await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
                correction_sweep = await _measure_auto_sub_combined_candidate(
                    delay_ms=best_sub1, job=job, candidate_index=1, total=1,
                    sweep_index_start=matrix_sweep_total + 3, sweep_total=matrix_sweep_total + 4,
                    stage="gain_correction_after", fc=fc, input_id=input_id,
                    mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
                    original_highpass=bool(_auto_sub_22_global_config(correction_snapshot).get("main_highpass_enabled", True)),
                    output_mode=OUTPUT_MODE_SUBWOOFER_22, original_config_snapshot=correction_snapshot,
                    sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2, active_subs=("sub1", "sub2"),
                )
                correction_after = _calculate_auto_sub_gain(
                    mode=OUTPUT_MODE_SUBWOOFER_22, target_curve=job.get("target_curve"),
                    anchor=job.get("main_target_anchor"), winner_curves={
                        "left": correction_sweep.get("calibrated_points_left") or [],
                        "right": correction_sweep.get("calibrated_points_right") or [],
                    }, crossover_hz=fc,
                )
                correction_verdict = _auto_sub_gain_verdict(gain_after, correction_after, OUTPUT_MODE_SUBWOOFER_22)
                if correction_verdict["accepted"]:
                    final_gain_deltas = {
                        "left": gain_deltas.get("left", 0.0) + correction_delta,
                        "right": gain_deltas.get("right", 0.0) + correction_delta,
                    }
                    final_gain_snapshot = correction_snapshot
                    final_gain_sweep = correction_sweep
                else:
                    set_audio_output_mode(
                        OUTPUT_MODE_SUBWOOFER_22, _auto_sub_22_global_config(gain_snapshot),
                        _auto_sub_22_candidate_subwoofers(
                            gain_snapshot, sub1_alignment_ms=best_sub1, sub2_alignment_ms=best_sub2,
                            active_subs=("sub1", "sub2"),
                        ),
                    )
                    if subwoofer_runtime is not None:
                        await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        _auto_sub_gain_log_line("AUTOGAIN_FEEDBACK", {
            "gain_after_step1": {
                "sub1": float(_auto_sub_22_sub(gain_snapshot, "sub1").get("level_db", 0.0)),
                "sub2": float(_auto_sub_22_sub(gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "score_before": _auto_sub_gain_log_score(job["auto_gain"]),
            "score_after_step1": _auto_sub_gain_log_score(gain_after),
            "response_per_db_left": (((correction_plan or {}).get("channels") or {}).get("left") or {}).get("response_change_per_db"),
            "response_per_db_right": (((correction_plan or {}).get("channels") or {}).get("right") or {}).get("response_change_per_db"),
            "remaining_error_left": (gain_after.get("channels", {}).get("left") or {}).get("target_delta_db"),
            "remaining_error_right": (gain_after.get("channels", {}).get("right") or {}).get("target_delta_db"),
            "raw_correction_db": ((correction_plan or {}).get("raw_deltas_db") or {}).get("left"),
            "applied_correction_db": ((correction_plan or {}).get("applied_deltas_db") or {}).get("left"),
            "correction_step_db": correction_deltas.get("left") if correction_deltas else None,
        })
        decision = "accepted_step2" if correction_verdict and correction_verdict.get("accepted") else (
            "accepted_step1" if gain_verdict.get("accepted") else "restored"
        )
        score_final_source = correction_after if decision == "accepted_step2" else (gain_after if decision == "accepted_step1" else job["auto_gain"])
        _auto_sub_gain_log_line("AUTOGAIN_RESULT", {
            "gain_final": {
                "sub1": float(_auto_sub_22_sub(final_gain_snapshot, "sub1").get("level_db", 0.0)),
                "sub2": float(_auto_sub_22_sub(final_gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "score_final": _auto_sub_gain_log_score(score_final_source), "decision": decision,
            "reason": ((correction_verdict or gain_verdict) or {}).get("reason"),
            "delay_final": {"sub1_ms": best_sub1, "sub2_ms": best_sub2},
        })
        job["auto_gain"].update({
            "applied": bool(gain_verdict["accepted"] and gain_deltas),
            "reverted": bool(gain_deltas and not gain_verdict["accepted"]),
            "verification": gain_after, "verification_verdict": gain_verdict,
            "response_correction": correction_plan,
            "correction_deltas_db": correction_deltas,
            "correction_verification": correction_after,
            "correction_verdict": correction_verdict,
            "final_deltas_db": final_gain_deltas,
            "original_levels_db": {
                "sub1": float(original_sub1.get("level_db", 0.0)), "sub2": float(original_sub2.get("level_db", 0.0)),
            },
            "final_levels_db": {
                "sub1": float(_auto_sub_22_sub(final_gain_snapshot, "sub1").get("level_db", 0.0)),
                "sub2": float(_auto_sub_22_sub(final_gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "stage_output_peaks": (final_gain_sweep or {}).get("stage_output_peaks"),
        })

        derived_delays: dict[str, Any] = {}
        try:
            config = SubwooferRuntimeConfig.from_overview(get_audio_output_overview())
            derived_delays = {
                "derived_main_delay_ms": round(config.derived_main_delay_ms, 2),
                "derived_sub1_delay_ms": round(config.derived_sub1_delay_ms, 2),
                "derived_sub2_delay_ms": round(config.derived_sub2_delay_ms, 2),
            }
        except Exception:
            derived_delays = {}

        job["combined_matrix"].update({
            "status": "completed",
            "winner": winner,
            "matrix_winner": matrix_scoring.get("matrix_winner"),
            "incumbent_winner": matrix_scoring.get("incumbent_winner"),
            "incumbent_score": matrix_scoring.get("incumbent_score"),
            "accepted_winner": matrix_scoring.get("accepted_winner"),
            "incumbent_accepted": matrix_scoring.get("incumbent_accepted"),
            "reject_reason": matrix_scoring.get("reject_reason"),
            "runner_up": matrix_scoring.get("runner_up"),
            "results": matrix_scoring["results"],
            "valid_count": len(matrix_valid),
        })
        _log_auto_sub_timing_summary(job)

        # Build baseline and confirmation measurements for before/after graph display
        all_22_sweeps = list(coarse1_results) + list(coarse2_results) + list(matrix_results)
        baseline_22_sweep = next(
            (r for r in all_22_sweeps
             if round(float(r.get("sub1_alignment_ms", r.get("delay_ms", 0.0))), 2) == round(float(original_sub1_alignment), 2)
             and round(float(r.get("sub2_alignment_ms", 0.0)), 2) == round(float(original_sub2_alignment), 2)
             and (_auto_sub_has_points(r, "points_left") or _auto_sub_has_points(r, "points_right"))),
            None,
        )
        confirm_22_sweep = final_gain_sweep if gain_verdict["accepted"] else next(
            (r for r in all_22_sweeps
             if round(float(r.get("sub1_alignment_ms", r.get("delay_ms", 0.0))), 2) == round(float(best_sub1), 2)
             and round(float(r.get("sub2_alignment_ms", 0.0)), 2) == round(float(best_sub2), 2)
             and (_auto_sub_has_points(r, "points_left") or _auto_sub_has_points(r, "points_right"))),
            None,
        )
        baseline_measurement = None
        confirmation_measurement = None
        _offset_db = _auto_sub_shared_bass_offset(
            baseline_22_sweep.get("points_left") if baseline_22_sweep else [],
            baseline_22_sweep.get("points_right") if baseline_22_sweep else [],
        )
        if baseline_22_sweep:
            baseline_measurement = _auto_sub_measurement_from_sweep(
                baseline_22_sweep, "Before", f"AutoSub 2.2 Baseline (S1 {original_sub1_alignment:.1f} / S2 {original_sub2_alignment:.1f} ms)",
                offset_db=_offset_db,
            )
        if confirm_22_sweep:
            confirmation_measurement = _auto_sub_measurement_from_sweep(
                confirm_22_sweep, "After", f"AutoSub 2.2 Optimized (S1 {best_sub1:.1f} / S2 {best_sub2:.1f} ms)",
                offset_db=_offset_db,
            )

        job["status"] = "completed"
        decision_label = "Kept 2.2 incumbent" if matrix_scoring.get("incumbent_accepted") else "Applied 2.2"
        job["message"] = (
            f"{decision_label}: Sub 1 {best_sub1:.2f} ms / Sub 2 {best_sub2:.2f} ms "
            f"(score {winner['score_pct']:.0f} %, {matrix_scoring.get('reject_reason')})"
        )
        job["result"] = {
            "mode": OUTPUT_MODE_SUBWOOFER_22,
            "original_sub1_alignment_ms": original_sub1_alignment,
            "original_sub2_alignment_ms": original_sub2_alignment,
            "suggested_sub1_alignment_ms": best_sub1,
            "suggested_sub2_alignment_ms": best_sub2,
            "applied_sub1_alignment_ms": best_sub1,
            "applied_sub2_alignment_ms": best_sub2,
            "applied": True,
            "auto_applied": True,
            "apply_decision": (
                "kept_22_incumbent"
                if matrix_scoring.get("incumbent_accepted")
                else "applied_22_combined_matrix"
            ),
            "crossover_hz": fc,
            "confidence": matrix_scoring.get("confidence", "uncertain"),
            "winner": winner,
            "matrix_winner": matrix_scoring.get("matrix_winner"),
            "incumbent_winner": matrix_scoring.get("incumbent_winner"),
            "incumbent_score": matrix_scoring.get("incumbent_score"),
            "accepted_winner": matrix_scoring.get("accepted_winner"),
            "incumbent_accepted": matrix_scoring.get("incumbent_accepted"),
            "reject_reason": matrix_scoring.get("reject_reason"),
            "sub1_coarse_winner": sub1_winner,
            "sub2_coarse_winner": sub2_winner,
            "runner_up": matrix_scoring.get("runner_up"),
            "ranking": matrix_scoring["results"],
            "combined_matrix": job["combined_matrix"],
            "candidate_ledger": candidate_ledger,
            "sweep_count": matrix_sweep_total + 2,
            "candidate_count": len(sub1_scan_delays) + len(sub2_scan_delays) + len(matrix_pairs),
            "sub1_coarse_candidate_count": len(sub1_scan_delays),
            "sub2_coarse_candidate_count": len(sub2_scan_delays),
            "matrix_candidate_count": len(matrix_pairs),
            "valid_count": len(matrix_valid),
            "sub1_coarse_valid_count": len(coarse1_valid),
            "sub2_coarse_valid_count": len(coarse2_valid),
            "baseline_measurement": baseline_measurement,
            "confirmation_measurement": confirmation_measurement,
            **derived_delays,
        }
        logger.info(
            "Auto-sub 2.2 optimize completed: fc=%sHz sub1 %.2f->%.2fms sub2 %.2f->%.2fms "
            "combined_score=%.0f%% score_L=%.1f%% score_R=%.1f%% confidence=%s",
            fc,
            original_sub1_alignment,
            best_sub1,
            original_sub2_alignment,
            best_sub2,
            winner.get("score_pct", 0),
            winner.get("score_L_pct", 0) or 0,
            winner.get("score_R_pct", 0) or 0,
            matrix_scoring.get("confidence", "uncertain"),
        )

    except Exception as exc:
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        logger.exception("Auto-sub 2.2 optimize failed")
        job["status"] = "failed"
        job["message"] = f"Auto Sub Optimize 2.2 failed: {exc}"
        job["error"] = {"detail": str(exc)}
        await _restore_original_config()

    finally:
        await _finish_auto_sub_worker(job, job_id)


async def _run_auto_sub_22_stereo_optimize(
    job_id: str,
    input_id: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    left_scan_delays: list[float],
    right_scan_delays: list[float],
    fc: int,
    original_config_snapshot: dict[str, Any],
    entry_epoch: int | None = None,
) -> None:
    from main import (
        measurement_sr_session,
        measurement_store,
        subwoofer_runtime,
    )
    from measurement_session import (
        MeasurementEntryInvalidated,
        _resolve_measurement_start_sample_rate,
    )
    global _auto_sub_lock
    from samplerate import _load_audio_output_mode, set_audio_output_mode

    job = _AUTO_SUB_JOBS.get(job_id)
    if not job:
        _auto_sub_lock.release()
        return

    async def _restore_original_config() -> None:
        await _restore_auto_sub_original_config(original_config_snapshot)

    original_left = _auto_sub_22_sub(original_config_snapshot, "sub1")
    original_right = _auto_sub_22_sub(original_config_snapshot, "sub2")
    original_left_alignment = float(original_left.get("alignment_ms", 0.0) or 0.0)
    original_right_alignment = float(original_right.get("alignment_ms", 0.0) or 0.0)

    def _valid(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [result for result in results if _auto_sub_has_points(result, "points")]

    def _reference_points(results: list[dict[str, Any]], delay_ms: float) -> list[list[float]] | None:
        valid = _valid(results)
        if not valid:
            return None
        reference = min(valid, key=lambda result: abs(float(result.get("delay_ms", 0.0) or 0.0) - delay_ms))
        points = reference.get("points") or []
        return points if isinstance(points, list) and len(points) >= 3 else None

    try:
        if measurement_sr_session is not None:
            try:
                await measurement_sr_session.register_auto_sub(job_id, entry_epoch=entry_epoch)
            except MeasurementEntryInvalidated:
                logger.info(
                    "AUTOSUB job=%s entry invalidated by measurement window close",
                    job_id,
                )
                job["status"] = "cancelled"
                job["message"] = "Auto Sub Optimize cancelled because the measurement window was closed."
                return
        if _auto_sub_cancel_requested(job):
            logger.info("AUTOSUB job=%s cancel observed (before sweeps)", job_id)
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        auto_sub_sweep_profile = _auto_sub_sweep_profile(fc)
        auto_sub_rate = _resolve_measurement_start_sample_rate()
        await _capture_auto_sub_main_references(
            job=job, fc=fc, input_id=input_id,
            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
            auto_sub_rate=auto_sub_rate, output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
            original_config_snapshot=original_config_snapshot,
        )
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        step_ms = _auto_sub_step_ms(fc)
        planned_left_fine_total = 6
        planned_right_fine_total = 6
        planned_sweep_total = (
            len(left_scan_delays)
            + planned_left_fine_total
            + len(right_scan_delays)
            + planned_right_fine_total
        )

        left_results: list[dict[str, Any]] = []
        job["stage"] = "left_sub"
        for idx, delay_ms in enumerate(left_scan_delays):
            sweep_index = idx + 1
            left_results.append(await _measure_auto_sub_candidate(
                delay_ms=delay_ms,
                job=job,
                candidate_index=sweep_index,
                total=planned_sweep_total,
                stage="left_sub",
                fc=fc,
                input_id=input_id,
                channel="left",
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate,
                original_level=0.0,
                original_polarity="normal",
                original_highpass=True,
                measurement_label=f"Optimizing Left Sub: L sweep {idx + 1}/{len(left_scan_delays)} @ {delay_ms:.2f} ms",
                candidate_current=idx + 1,
                candidate_total=len(left_scan_delays),
                measure_channel="left",
                output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=delay_ms,
                sub2_alignment_ms=original_right_alignment,
                active_subs=("sub1",),
            ))
            if isinstance(job.get("progress"), dict):
                job["progress"]["sweep_current"] = sweep_index
                job["progress"]["sweep_total"] = planned_sweep_total
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        left_valid = _valid(left_results)
        if not left_valid:
            job["status"] = "failed"
            job["message"] = "No valid Left Sub sweep results to score"
            job["error"] = {"detail": "Left Sub sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return
        left_coarse_scoring = score_sub_alignment_candidates(
            left_valid,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_left_alignment,
        )
        _auto_sub_rank_results(left_coarse_scoring["results"])
        left_coarse_winner = left_coarse_scoring["winner"]
        left_coarse_runner_up = left_coarse_scoring.get("runner_up")
        left_fine_delays = _auto_sub_fine_delay_candidates(
            left_coarse_winner,
            left_coarse_runner_up,
            step_ms,
            {round(float(delay), 2) for delay in left_scan_delays},
        )
        left_fine_results: list[dict[str, Any]] = []
        left_fine_valid: list[dict[str, Any]] = []
        left_fine_scoring: dict[str, Any] | None = None
        left_fine_winner: dict[str, Any] | None = None
        left_low_guard_reference_points = _reference_points(left_valid, original_left_alignment)
        job["fine_scan"] = {
            "enabled": True,
            "triggered": bool(left_fine_delays),
            "status": "left_running" if left_fine_delays else "left_skipped",
            "fine_step_ms": step_ms / 4.0,
            "left": {
                "status": "running" if left_fine_delays else "skipped",
                "coarse_winner": left_coarse_winner,
                "coarse_runner_up": left_coarse_runner_up,
                "candidates": left_fine_delays,
            },
            "right": {"status": "pending", "candidates": []},
        }
        if left_fine_delays:
            job["stage"] = "left_fine"
            for idx, delay_ms in enumerate(left_fine_delays):
                sweep_index = len(left_scan_delays) + idx + 1
                left_fine_results.append(await _measure_auto_sub_candidate(
                    delay_ms=delay_ms,
                    job=job,
                    candidate_index=sweep_index,
                    total=planned_sweep_total,
                    stage="left_fine",
                    fc=fc,
                    input_id=input_id,
                    channel="left",
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate,
                    original_level=0.0,
                    original_polarity="normal",
                    original_highpass=True,
                    measurement_label=f"Optimizing Left Sub Fine: L sweep {idx + 1}/{len(left_fine_delays)} @ {delay_ms:.2f} ms",
                    candidate_current=idx + 1,
                    candidate_total=len(left_fine_delays),
                    measure_channel="left",
                    output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                    original_config_snapshot=original_config_snapshot,
                    sub1_alignment_ms=delay_ms,
                    sub2_alignment_ms=original_right_alignment,
                    active_subs=("sub1",),
                ))
                if isinstance(job.get("progress"), dict):
                    job["progress"]["sweep_current"] = sweep_index
                    job["progress"]["sweep_total"] = planned_sweep_total
                if _auto_sub_cancel_requested(job):
                    job["message"] = "Auto Sub Optimize cancelled."
                    await _restore_original_config()
                    return
            left_fine_valid = _valid(left_fine_results)
            if left_fine_valid:
                left_fine_scoring = score_sub_alignment_candidates(
                    left_fine_valid,
                    crossover_hz=fc,
                    low_guard_reference_points=left_low_guard_reference_points,
                    low_guard_reference_delay_ms=original_left_alignment,
                )
                _auto_sub_rank_results(left_fine_scoring["results"])
                left_fine_winner = left_fine_scoring["winner"]
                job["fine_scan"]["left"].update({
                    "status": "completed",
                    "winner": left_fine_winner,
                    "runner_up": left_fine_scoring.get("runner_up"),
                    "results": left_fine_scoring["results"],
                    "valid_count": len(left_fine_valid),
                    "sweep_count": len(left_fine_delays),
                })
            else:
                job["fine_scan"]["left"].update({
                    "status": "no_valid_results",
                    "winner": None,
                    "runner_up": None,
                    "results": left_fine_results,
                    "valid_count": 0,
                    "sweep_count": len(left_fine_delays),
                })

        left_final_valid = left_valid + left_fine_valid
        left_scoring = score_sub_alignment_candidates(
            left_final_valid,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_left_alignment,
        )
        _auto_sub_rank_results(left_scoring["results"])
        left_scan_by_delay: dict[float, str] = {}
        for result in left_valid:
            left_scan_by_delay[_auto_sub_delay_key(result)] = "coarse"
        for result in left_fine_valid:
            left_scan_by_delay[_auto_sub_delay_key(result)] = "fine"
        for result in left_scoring["results"]:
            result["scan"] = left_scan_by_delay.get(_auto_sub_delay_key(result), result.get("scan", "coarse"))
        left_coarse_accepted_candidate = _auto_sub_best_scan_result(left_scoring["results"], "coarse") or left_coarse_winner
        left_fine_accepted_candidate = _auto_sub_best_scan_result(left_scoring["results"], "fine")
        left_incumbent_winner = _auto_sub_result_for_delay(left_scoring["results"], original_left_alignment)
        left_acceptance = _auto_sub_select_accepted_winner(
            coarse_winner=left_coarse_accepted_candidate,
            fine_winner=left_fine_accepted_candidate,
            incumbent_winner=left_incumbent_winner,
        )
        left_winner = left_acceptance["accepted_winner"]
        best_left = _auto_sub_clamped_delay(float(left_winner.get("delay_ms", original_left_alignment) or original_left_alignment))
        job["fine_scan"]["left"]["final_winner"] = left_winner
        job["fine_scan"]["left"]["final_results"] = left_scoring["results"]
        job["fine_scan"]["left"]["accepted_winner"] = left_winner
        job["fine_scan"]["left"]["fine_accepted"] = left_acceptance["fine_accepted"]
        job["fine_scan"]["left"]["reject_reason"] = left_acceptance["reject_reason"]
        job["fine_scan"]["left"]["incumbent_winner"] = left_incumbent_winner
        job["fine_scan"]["left"]["incumbent_score"] = left_acceptance["incumbent_score"]
        job["fine_scan"]["status"] = "right_pending"

        right_results: list[dict[str, Any]] = []
        job["stage"] = "right_sub"
        for idx, delay_ms in enumerate(right_scan_delays):
            sweep_index = len(left_scan_delays) + len(left_fine_delays) + idx + 1
            right_results.append(await _measure_auto_sub_candidate(
                delay_ms=delay_ms,
                job=job,
                candidate_index=sweep_index,
                total=planned_sweep_total,
                stage="right_sub",
                fc=fc,
                input_id=input_id,
                channel="right",
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_ref=calibration_ref,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile,
                auto_sub_rate=auto_sub_rate,
                original_level=0.0,
                original_polarity="normal",
                original_highpass=True,
                measurement_label=f"Optimizing Right Sub: R sweep {idx + 1}/{len(right_scan_delays)} @ {delay_ms:.2f} ms",
                candidate_current=idx + 1,
                candidate_total=len(right_scan_delays),
                measure_channel="right",
                output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=best_left,
                sub2_alignment_ms=delay_ms,
                active_subs=("sub2",),
            ))
            if isinstance(job.get("progress"), dict):
                job["progress"]["sweep_current"] = sweep_index
                job["progress"]["sweep_total"] = planned_sweep_total
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

        right_valid = _valid(right_results)
        if not right_valid:
            job["status"] = "failed"
            job["message"] = "No valid Right Sub sweep results to score"
            job["error"] = {"detail": "Right Sub sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return
        right_coarse_scoring = score_sub_alignment_candidates(
            right_valid,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_right_alignment,
        )
        _auto_sub_rank_results(right_coarse_scoring["results"])
        right_coarse_winner = right_coarse_scoring["winner"]
        right_coarse_runner_up = right_coarse_scoring.get("runner_up")
        right_fine_delays = _auto_sub_fine_delay_candidates(
            right_coarse_winner,
            right_coarse_runner_up,
            step_ms,
            {round(float(delay), 2) for delay in right_scan_delays},
        )
        right_fine_results: list[dict[str, Any]] = []
        right_fine_valid: list[dict[str, Any]] = []
        right_fine_scoring: dict[str, Any] | None = None
        right_fine_winner: dict[str, Any] | None = None
        right_low_guard_reference_points = _reference_points(right_valid, original_right_alignment)
        actual_sweep_total = (
            len(left_scan_delays)
            + len(left_fine_delays)
            + len(right_scan_delays)
            + len(right_fine_delays)
        )
        job["fine_scan"].update({
            "triggered": bool(left_fine_delays or right_fine_delays),
            "status": "right_running" if right_fine_delays else "right_skipped",
        })
        job["fine_scan"]["right"] = {
            "status": "running" if right_fine_delays else "skipped",
            "coarse_winner": right_coarse_winner,
            "coarse_runner_up": right_coarse_runner_up,
            "candidates": right_fine_delays,
        }
        if right_fine_delays:
            job["stage"] = "right_fine"
            for idx, delay_ms in enumerate(right_fine_delays):
                sweep_index = len(left_scan_delays) + len(left_fine_delays) + len(right_scan_delays) + idx + 1
                right_fine_results.append(await _measure_auto_sub_candidate(
                    delay_ms=delay_ms,
                    job=job,
                    candidate_index=sweep_index,
                    total=actual_sweep_total,
                    stage="right_fine",
                    fc=fc,
                    input_id=input_id,
                    channel="right",
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate,
                    original_level=0.0,
                    original_polarity="normal",
                    original_highpass=True,
                    measurement_label=f"Optimizing Right Sub Fine: R sweep {idx + 1}/{len(right_fine_delays)} @ {delay_ms:.2f} ms",
                    candidate_current=idx + 1,
                    candidate_total=len(right_fine_delays),
                    measure_channel="right",
                    output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                    original_config_snapshot=original_config_snapshot,
                    sub1_alignment_ms=best_left,
                    sub2_alignment_ms=delay_ms,
                    active_subs=("sub2",),
                ))
                if isinstance(job.get("progress"), dict):
                    job["progress"]["sweep_current"] = sweep_index
                    job["progress"]["sweep_total"] = actual_sweep_total
                if _auto_sub_cancel_requested(job):
                    job["message"] = "Auto Sub Optimize cancelled."
                    await _restore_original_config()
                    return
            right_fine_valid = _valid(right_fine_results)
            if right_fine_valid:
                right_fine_scoring = score_sub_alignment_candidates(
                    right_fine_valid,
                    crossover_hz=fc,
                    low_guard_reference_points=right_low_guard_reference_points,
                    low_guard_reference_delay_ms=original_right_alignment,
                )
                _auto_sub_rank_results(right_fine_scoring["results"])
                right_fine_winner = right_fine_scoring["winner"]
                job["fine_scan"]["right"].update({
                    "status": "completed",
                    "winner": right_fine_winner,
                    "runner_up": right_fine_scoring.get("runner_up"),
                    "results": right_fine_scoring["results"],
                    "valid_count": len(right_fine_valid),
                    "sweep_count": len(right_fine_delays),
                })
            else:
                job["fine_scan"]["right"].update({
                    "status": "no_valid_results",
                    "winner": None,
                    "runner_up": None,
                    "results": right_fine_results,
                    "valid_count": 0,
                    "sweep_count": len(right_fine_delays),
                })

        right_final_valid = right_valid + right_fine_valid
        right_scoring = score_sub_alignment_candidates(
            right_final_valid,
            crossover_hz=fc,
            low_guard_reference_delay_ms=original_right_alignment,
        )
        _auto_sub_rank_results(right_scoring["results"])
        right_scan_by_delay: dict[float, str] = {}
        for result in right_valid:
            right_scan_by_delay[_auto_sub_delay_key(result)] = "coarse"
        for result in right_fine_valid:
            right_scan_by_delay[_auto_sub_delay_key(result)] = "fine"
        for result in right_scoring["results"]:
            result["scan"] = right_scan_by_delay.get(_auto_sub_delay_key(result), result.get("scan", "coarse"))
        right_coarse_accepted_candidate = _auto_sub_best_scan_result(right_scoring["results"], "coarse") or right_coarse_winner
        right_fine_accepted_candidate = _auto_sub_best_scan_result(right_scoring["results"], "fine")
        right_incumbent_winner = _auto_sub_result_for_delay(right_scoring["results"], original_right_alignment)
        right_acceptance = _auto_sub_select_accepted_winner(
            coarse_winner=right_coarse_accepted_candidate,
            fine_winner=right_fine_accepted_candidate,
            incumbent_winner=right_incumbent_winner,
        )
        right_winner = right_acceptance["accepted_winner"]
        best_right = _auto_sub_clamped_delay(float(right_winner.get("delay_ms", original_right_alignment) or original_right_alignment))
        job["fine_scan"]["right"]["final_winner"] = right_winner
        job["fine_scan"]["right"]["final_results"] = right_scoring["results"]
        job["fine_scan"]["right"]["accepted_winner"] = right_winner
        job["fine_scan"]["right"]["fine_accepted"] = right_acceptance["fine_accepted"]
        job["fine_scan"]["right"]["reject_reason"] = right_acceptance["reject_reason"]
        job["fine_scan"]["right"]["incumbent_winner"] = right_incumbent_winner
        job["fine_scan"]["right"]["incumbent_score"] = right_acceptance["incumbent_score"]
        job["fine_scan"]["status"] = "completed"

        candidate_ledger = (
            _auto_sub_candidate_ledger(
                left_results, left_scoring, mode="2.2_stereo", phase="left_coarse", channel="left",
                roles={
                    "coarse_winner": left_coarse_accepted_candidate,
                    "final_accepted_winner": left_winner,
                },
                requested_incumbent={"delay_ms": original_left_alignment},
            )
            + _auto_sub_candidate_ledger(
                left_fine_results, left_scoring, mode="2.2_stereo", phase="left_fine", channel="left",
                roles={"fine_winner": left_fine_accepted_candidate, "final_accepted_winner": left_winner},
            )
            + _auto_sub_candidate_ledger(
                right_results, right_scoring, mode="2.2_stereo", phase="right_coarse", channel="right",
                roles={
                    "coarse_winner": right_coarse_accepted_candidate,
                    "final_accepted_winner": right_winner,
                },
                requested_incumbent={"delay_ms": original_right_alignment},
            )
            + _auto_sub_candidate_ledger(
                right_fine_results, right_scoring, mode="2.2_stereo", phase="right_fine", channel="right",
                roles={"fine_winner": right_fine_accepted_candidate, "final_accepted_winner": right_winner},
            )
        )

        apply_ok = False
        try:
            sub_config = _auto_sub_22_global_config(original_config_snapshot)
            subwoofers_config = _auto_sub_22_candidate_subwoofers(
                original_config_snapshot,
                sub1_alignment_ms=best_left,
                sub2_alignment_ms=best_right,
                active_subs=("sub1", "sub2"),
            )
            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_22_STEREO, sub_config, subwoofers_config)
            if subwoofer_runtime is not None:
                config = SubwooferRuntimeConfig.from_overview(get_audio_output_overview())
                await subwoofer_runtime.sync(config)
            await asyncio.sleep(0.3)
            apply_ok = _auto_sub_22_verify_alignment(_load_audio_output_mode(), best_left, best_right)
        except Exception:
            logger.exception("Auto-sub 2.2 Stereo Bass: failed to apply winner pair %.2f / %.2f ms", best_left, best_right)

        if not apply_ok:
            job["status"] = "failed"
            job["message"] = f"Scoring succeeded but failed to apply Left/Right pair {best_left:.2f} / {best_right:.2f} ms"
            job["error"] = {"detail": "Winner apply failed - original config restored"}
            await _restore_original_config()
            return

        derived_delays: dict[str, Any] = {}
        try:
            config = SubwooferRuntimeConfig.from_overview(get_audio_output_overview())
            derived_delays = {
                "derived_main_delay_ms": round(config.derived_main_delay_ms, 2),
                "derived_sub1_delay_ms": round(config.derived_sub1_delay_ms, 2),
                "derived_sub2_delay_ms": round(config.derived_sub2_delay_ms, 2),
            }
        except Exception:
            derived_delays = {}

        left_score = float(left_winner.get("score", 0.0) or 0.0)
        right_score = float(right_winner.get("score", 0.0) or 0.0)
        gain_left_winner = _auto_sub_result_for_delay(list(left_results) + list(left_fine_results), best_left) or {}
        gain_right_winner = _auto_sub_result_for_delay(list(right_results) + list(right_fine_results), best_right) or {}
        selected_left_polarity = str(original_left.get("polarity", "normal"))
        selected_right_polarity = str(original_right.get("polarity", "normal"))
        stereo_polarity: dict[str, Any] = {}

        async def _check_stereo_polarity(
            side: str, incumbent: dict[str, Any], delay: float, incumbent_polarity: str,
        ) -> tuple[dict[str, Any], float, str, dict[str, Any]]:
            opposite = _auto_sub_opposite_polarity(incumbent_polarity)
            is_left = side == "left"
            alt = await _measure_auto_sub_candidate(
                delay_ms=delay, job=job, candidate_index=1, total=1, stage=f"{side}_polarity_check", fc=fc,
                input_id=input_id, channel=side, mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                original_level=0.0, original_polarity="normal", original_highpass=True,
                measure_channel=side, output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                original_config_snapshot=original_config_snapshot,
                sub1_alignment_ms=delay if is_left else best_left,
                sub2_alignment_ms=best_right if is_left else delay,
                active_subs=("sub1",) if is_left else ("sub2",),
                sub1_polarity=opposite if is_left else selected_left_polarity,
                sub2_polarity=selected_right_polarity if is_left else opposite,
            )
            rows = [dict(incumbent, delay_ms=0.0, points=incumbent.get("points") or []), dict(alt, delay_ms=1.0)]
            scoring = score_sub_alignment_candidates(rows, crossover_hz=fc, low_guard_reference_delay_ms=0.0)
            scored_incumbent = _auto_sub_result_for_delay(scoring["results"], 0.0) or {}
            scored_alt = _auto_sub_result_for_delay(scoring["results"], 1.0) or {}
            decision = _auto_sub_polarity_decision(scored_incumbent, scored_alt)
            decision.update({"incumbent": incumbent_polarity, "alternative": opposite, "selected": incumbent_polarity})
            if not decision["accepted"]:
                return incumbent, delay, incumbent_polarity, decision
            local_step = _auto_sub_step_ms(fc) / 4.0
            local_rows = [alt]
            for idx, candidate_delay in enumerate([
                _auto_sub_clamped_delay(delay - 2 * local_step), _auto_sub_clamped_delay(delay - local_step),
                _auto_sub_clamped_delay(delay + local_step), _auto_sub_clamped_delay(delay + 2 * local_step),
            ]):
                local_rows.append(await _measure_auto_sub_candidate(
                    delay_ms=candidate_delay, job=job, candidate_index=idx + 1, total=4,
                    stage=f"{side}_polarity_fine", fc=fc, input_id=input_id, channel=side,
                    mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
                    original_highpass=True, measure_channel=side,
                    output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO, original_config_snapshot=original_config_snapshot,
                    sub1_alignment_ms=candidate_delay if is_left else best_left,
                    sub2_alignment_ms=best_right if is_left else candidate_delay,
                    active_subs=("sub1",) if is_left else ("sub2",),
                    sub1_polarity=opposite if is_left else selected_left_polarity,
                    sub2_polarity=selected_right_polarity if is_left else opposite,
                ))
            fine_scoring = score_sub_alignment_candidates(local_rows, crossover_hz=fc)
            fine_winner = fine_scoring["winner"]
            selected = _auto_sub_result_for_delay(local_rows, float(fine_winner["delay_ms"])) or alt
            decision.update({"selected": opposite, "fine_scan": {"candidate_count": 4, "winner": fine_winner}})
            return selected, float(fine_winner["delay_ms"]), opposite, decision

        gain_left_winner, best_left, selected_left_polarity, stereo_polarity["left"] = await _check_stereo_polarity(
            "left", gain_left_winner, best_left, selected_left_polarity,
        )
        gain_right_winner, best_right, selected_right_polarity, stereo_polarity["right"] = await _check_stereo_polarity(
            "right", gain_right_winner, best_right, selected_right_polarity,
        )
        polarity_snapshot = _auto_sub_snapshot_copy(original_config_snapshot)
        polarity_snapshot.setdefault("subwoofers", {}).setdefault("sub1", {})["polarity"] = selected_left_polarity
        polarity_snapshot.setdefault("subwoofers", {}).setdefault("sub2", {})["polarity"] = selected_right_polarity
        job["polarity_check"] = stereo_polarity
        job["auto_gain"] = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
            target_curve=job.get("target_curve"), anchor=job.get("main_target_anchor"),
            winner_curves={
                "left": gain_left_winner.get("calibrated_points") or [],
                "right": gain_right_winner.get("calibrated_points") or [],
            }, crossover_hz=fc,
        )
        logger.info("AUTOSUB_GAIN mode=2.2_stereo diagnostics=%s", json.dumps(job["auto_gain"], sort_keys=True))
        gain_deltas = _auto_sub_gain_deltas(job["auto_gain"], OUTPUT_MODE_SUBWOOFER_22_STEREO, max_abs_db=6.0)
        _auto_sub_gain_log_line("AUTOGAIN_INIT", {
            "mode": OUTPUT_MODE_SUBWOOFER_22_STEREO, "xo_hz": fc,
            "target": (job.get("target_curve") or {}).get("label"),
            "anchor_hz": (job.get("main_target_anchor") or {}).get("usable_band_hz"),
            "target_offset_db": (job.get("main_target_anchor") or {}).get("target_vertical_offset_db"),
            "gain_before": {"left": float(original_left.get("level_db", 0.0)), "right": float(original_right.get("level_db", 0.0))},
            "winner_delta_left": (job["auto_gain"].get("channels", {}).get("left") or {}).get("target_delta_db"),
            "winner_delta_right": (job["auto_gain"].get("channels", {}).get("right") or {}).get("target_delta_db"),
            "combined_delta_db": None, "first_step_db": gain_deltas,
        })
        gain_snapshot = _auto_sub_22_snapshot_with_gain(
            polarity_snapshot,
            left_delta_db=gain_deltas.get("left", 0.0), right_delta_db=gain_deltas.get("right", 0.0),
        )
        if gain_deltas:
            set_audio_output_mode(
                OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(gain_snapshot),
                _auto_sub_22_candidate_subwoofers(
                    gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                    active_subs=("sub1", "sub2"),
                ),
            )
            if subwoofer_runtime is not None:
                await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        gain_after_left = await _measure_auto_sub_candidate(
            delay_ms=best_left, job=job, candidate_index=1, total=2, stage="gain_after", fc=fc,
            input_id=input_id, channel="left", mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
            calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
            auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
            original_level=0.0, original_polarity="normal", original_highpass=True,
            measure_channel="left", output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
            original_config_snapshot=gain_snapshot, sub1_alignment_ms=best_left,
            sub2_alignment_ms=best_right, active_subs=("sub1", "sub2"),
        )
        gain_after_right = await _measure_auto_sub_candidate(
            delay_ms=best_right, job=job, candidate_index=2, total=2, stage="gain_after", fc=fc,
            input_id=input_id, channel="right", mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
            calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
            auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
            original_level=0.0, original_polarity="normal", original_highpass=True,
            measure_channel="right", output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
            original_config_snapshot=gain_snapshot, sub1_alignment_ms=best_left,
            sub2_alignment_ms=best_right, active_subs=("sub1", "sub2"),
        )
        gain_after = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_22_STEREO, target_curve=job.get("target_curve"),
            anchor=job.get("main_target_anchor"), winner_curves={
                "left": gain_after_left.get("calibrated_points") or [],
                "right": gain_after_right.get("calibrated_points") or [],
            }, crossover_hz=fc,
        )
        gain_verdict = _auto_sub_gain_verdict(job["auto_gain"], gain_after, OUTPUT_MODE_SUBWOOFER_22_STEREO)
        accepted_step1_sides = {
            side: bool((gain_verdict.get("channels", {}).get(side) or {}).get("accepted"))
            for side in ("left", "right")
        }
        retained_step1_deltas = {
            side: gain_deltas.get(side, 0.0) if accepted_step1_sides[side] else 0.0
            for side in ("left", "right")
        }
        step1_retained = any(accepted_step1_sides.values())
        final_gain_deltas = retained_step1_deltas
        final_gain_snapshot = _auto_sub_22_snapshot_with_gain(
            polarity_snapshot,
            left_delta_db=retained_step1_deltas["left"],
            right_delta_db=retained_step1_deltas["right"],
        )
        final_gain_left = gain_after_left if accepted_step1_sides["left"] else gain_left_winner
        final_gain_right = gain_after_right if accepted_step1_sides["right"] else gain_right_winner
        correction_deltas: dict[str, float] = {}
        correction_plan = None
        correction_after = None
        correction_verdict = None
        stereo_probe_plan = None
        if not step1_retained:
            set_audio_output_mode(
                OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(original_config_snapshot),
                _auto_sub_22_candidate_subwoofers(
                    original_config_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                    active_subs=("sub1", "sub2"),
                ),
            )
            if subwoofer_runtime is not None:
                await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        elif not all(accepted_step1_sides.values()):
            # Stereo channels have independent Gain controls.  A regression on
            # one side must not discard a measured improvement on the other.
            set_audio_output_mode(
                OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(final_gain_snapshot),
                _auto_sub_22_candidate_subwoofers(
                    final_gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                    active_subs=("sub1", "sub2"),
                ),
            )
            if subwoofer_runtime is not None:
                await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
            correction_verdict = {
                "accepted": False,
                "reason": "Retained improved Stereo side; restored regressed side",
                "channels": gain_verdict.get("channels", {}),
                "step1_retained": True,
            }
        else:
            correction_plan = _auto_sub_gain_response_correction(
                job["auto_gain"], gain_after, gain_deltas, OUTPUT_MODE_SUBWOOFER_22_STEREO,
            )
            correction_deltas = correction_plan.get("deltas_db") or {}
            if not correction_plan.get("available"):
                stereo_probe_plan = _auto_sub_stereo_probe_plan(
                    correction_plan=correction_plan, gain_after=gain_after, gain_deltas=gain_deltas,
                    accepted_step1_sides=accepted_step1_sides,
                    after_points={
                        "left": gain_after_left.get("calibrated_points") or [],
                        "right": gain_after_right.get("calibrated_points") or [],
                    },
                    target_curve=job.get("target_curve"), anchor=job.get("main_target_anchor"),
                    crossover_hz=fc,
                )
                correction_deltas = stereo_probe_plan.get("deltas_db") or {}
                if not stereo_probe_plan.get("available"):
                    correction_verdict = {
                        "accepted": False,
                        "reason": correction_plan.get("reason"),
                        "channels": {},
                        "step1_retained": True,
                    }
            if any(abs(value) > 0.0005 for value in correction_deltas.values()):
                correction_snapshot = _auto_sub_22_snapshot_with_gain(
                    gain_snapshot, left_delta_db=correction_deltas.get("left", 0.0),
                    right_delta_db=correction_deltas.get("right", 0.0),
                )
                set_audio_output_mode(
                    OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(correction_snapshot),
                    _auto_sub_22_candidate_subwoofers(
                        correction_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                        active_subs=("sub1", "sub2"),
                    ),
                )
                if subwoofer_runtime is not None:
                    await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
                correction_left = await _measure_auto_sub_candidate(
                    delay_ms=best_left, job=job, candidate_index=1, total=2,
                    stage="gain_correction_after", fc=fc, input_id=input_id, channel="left",
                    mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
                    original_highpass=True, measure_channel="left",
                    output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                    original_config_snapshot=correction_snapshot, sub1_alignment_ms=best_left,
                    sub2_alignment_ms=best_right, active_subs=("sub1", "sub2"),
                )
                correction_right = await _measure_auto_sub_candidate(
                    delay_ms=best_right, job=job, candidate_index=2, total=2,
                    stage="gain_correction_after", fc=fc, input_id=input_id, channel="right",
                    mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate, original_level=0.0, original_polarity="normal",
                    original_highpass=True, measure_channel="right",
                    output_mode=OUTPUT_MODE_SUBWOOFER_22_STEREO,
                    original_config_snapshot=correction_snapshot, sub1_alignment_ms=best_left,
                    sub2_alignment_ms=best_right, active_subs=("sub1", "sub2"),
                )
                correction_after = _calculate_auto_sub_gain(
                    mode=OUTPUT_MODE_SUBWOOFER_22_STEREO, target_curve=job.get("target_curve"),
                    anchor=job.get("main_target_anchor"), winner_curves={
                        "left": correction_left.get("calibrated_points") or [],
                        "right": correction_right.get("calibrated_points") or [],
                    }, crossover_hz=fc,
                )
                if stereo_probe_plan and stereo_probe_plan.get("available"):
                    probe_channels: dict[str, Any] = {}
                    accepted_probe_sides: dict[str, bool] = {}
                    correction_points = {
                        "left": correction_left.get("calibrated_points") or [],
                        "right": correction_right.get("calibrated_points") or [],
                    }
                    for side in ("left", "right"):
                        planned = side in correction_deltas
                        before_corridor = (((stereo_probe_plan.get("channels") or {}).get(side) or {}).get("corridor_before") or {})
                        after_corridor = _auto_sub_stereo_corridor_violation(
                            points=correction_points[side], target_curve=job.get("target_curve"),
                            anchor=job.get("main_target_anchor"), crossover_hz=fc,
                            direction=correction_deltas.get(side, 0.0),
                        ) if planned else before_corridor
                        before_score = abs(float(gain_after["channels"][side]["target_delta_db"]))
                        after_score = abs(float(correction_after["channels"][side]["target_delta_db"]))
                        score_better = after_score < before_score
                        corridor_better = (
                            after_corridor.get("available") is True
                            and float(after_corridor.get("severity_db", 0.0)) < float(before_corridor.get("severity_db", 0.0))
                        )
                        accepted_probe_sides[side] = bool(planned and score_better and corridor_better)
                        probe_channels[side] = {
                            "planned": planned, "accepted": accepted_probe_sides[side],
                            "score_before": round(before_score, 3), "score_after": round(after_score, 3),
                            "score_better": score_better, "corridor_before": before_corridor,
                            "corridor_after": after_corridor, "corridor_better": corridor_better,
                        }
                    accepted_any_probe = any(accepted_probe_sides.values())
                    final_gain_deltas = {
                        side: gain_deltas.get(side, 0.0) + (
                            correction_deltas.get(side, 0.0) if accepted_probe_sides[side] else 0.0
                        ) for side in ("left", "right")
                    }
                    final_gain_snapshot = _auto_sub_22_snapshot_with_gain(
                        polarity_snapshot, left_delta_db=final_gain_deltas["left"],
                        right_delta_db=final_gain_deltas["right"],
                    )
                    final_gain_left = correction_left if accepted_probe_sides["left"] else gain_after_left
                    final_gain_right = correction_right if accepted_probe_sides["right"] else gain_after_right
                    correction_verdict = {
                        "accepted": accepted_any_probe,
                        "reason": "Stereo corridor probe improved score and 1/3-octave violation" if accepted_any_probe else "Stereo corridor probe rejected; Step 1 retained",
                        "channels": probe_channels, "step1_retained": True, "stereo_probe": True,
                    }
                    if not all(accepted_probe_sides.get(side, False) for side in correction_deltas):
                        set_audio_output_mode(
                            OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(final_gain_snapshot),
                            _auto_sub_22_candidate_subwoofers(
                                final_gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                                active_subs=("sub1", "sub2"),
                            ),
                        )
                        if subwoofer_runtime is not None:
                            await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
                else:
                    correction_verdict = _auto_sub_gain_verdict(
                        gain_after, correction_after, OUTPUT_MODE_SUBWOOFER_22_STEREO,
                    )
                    if correction_verdict["accepted"]:
                        final_gain_deltas = {
                            side: gain_deltas.get(side, 0.0) + correction_deltas.get(side, 0.0)
                            for side in ("left", "right")
                        }
                        final_gain_snapshot = correction_snapshot
                        final_gain_left, final_gain_right = correction_left, correction_right
                    else:
                        set_audio_output_mode(
                            OUTPUT_MODE_SUBWOOFER_22_STEREO, _auto_sub_22_global_config(gain_snapshot),
                            _auto_sub_22_candidate_subwoofers(
                                gain_snapshot, sub1_alignment_ms=best_left, sub2_alignment_ms=best_right,
                                active_subs=("sub1", "sub2"),
                            ),
                        )
                        if subwoofer_runtime is not None:
                            await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        _auto_sub_gain_log_line("AUTOGAIN_FEEDBACK", {
            "gain_after_step1": {
                "left": float(_auto_sub_22_sub(gain_snapshot, "sub1").get("level_db", 0.0)),
                "right": float(_auto_sub_22_sub(gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "score_before": _auto_sub_gain_log_score(job["auto_gain"]),
            "score_after_step1": _auto_sub_gain_log_score(gain_after),
            "response_per_db_left": (((correction_plan or {}).get("channels") or {}).get("left") or {}).get("response_change_per_db"),
            "response_per_db_right": (((correction_plan or {}).get("channels") or {}).get("right") or {}).get("response_change_per_db"),
            "remaining_error_left": (gain_after.get("channels", {}).get("left") or {}).get("target_delta_db"),
            "remaining_error_right": (gain_after.get("channels", {}).get("right") or {}).get("target_delta_db"),
            "raw_correction_db": (correction_plan or {}).get("raw_deltas_db") or None,
            "applied_correction_db": (correction_plan or {}).get("applied_deltas_db") or None,
            "correction_step_db": correction_deltas or None,
        })
        decision = "accepted_step2" if correction_verdict and correction_verdict.get("accepted") else (
            "accepted_step1" if step1_retained else "restored"
        )
        if decision == "accepted_step2":
            if correction_verdict.get("stereo_probe"):
                score_final_source = copy.deepcopy(gain_after)
                for side in ("left", "right"):
                    if ((correction_verdict.get("channels") or {}).get(side) or {}).get("accepted"):
                        score_final_source["channels"][side] = copy.deepcopy(correction_after["channels"][side])
            else:
                score_final_source = correction_after
        elif decision == "accepted_step1":
            score_final_source = copy.deepcopy(job["auto_gain"])
            for side in ("left", "right"):
                if accepted_step1_sides[side]:
                    score_final_source["channels"][side] = copy.deepcopy(gain_after["channels"][side])
        else:
            score_final_source = job["auto_gain"]
        _auto_sub_gain_log_line("AUTOGAIN_RESULT", {
            "gain_final": {
                "left": float(_auto_sub_22_sub(final_gain_snapshot, "sub1").get("level_db", 0.0)),
                "right": float(_auto_sub_22_sub(final_gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "score_final": _auto_sub_gain_log_score(score_final_source), "decision": decision,
            "reason": ((correction_verdict or gain_verdict) or {}).get("reason"),
            "delay_final": {"left_ms": best_left, "right_ms": best_right},
        })
        job["auto_gain"].update({
            "applied": bool(step1_retained and gain_deltas),
            "reverted": bool(gain_deltas and not step1_retained),
            "accepted_step1_sides": accepted_step1_sides,
            "verification": gain_after, "verification_verdict": gain_verdict,
            "response_correction": correction_plan,
            "stereo_corridor_probe": stereo_probe_plan,
            "correction_deltas_db": correction_deltas,
            "correction_verification": correction_after,
            "correction_verdict": correction_verdict,
            "final_deltas_db": final_gain_deltas,
            "original_levels_db": {
                "sub1": float(original_left.get("level_db", 0.0)), "sub2": float(original_right.get("level_db", 0.0)),
            },
            "final_levels_db": {
                "sub1": float(_auto_sub_22_sub(final_gain_snapshot, "sub1").get("level_db", 0.0)),
                "sub2": float(_auto_sub_22_sub(final_gain_snapshot, "sub2").get("level_db", 0.0)),
            },
            "stage_output_peaks": {
                "left": final_gain_left.get("stage_output_peaks"),
                "right": final_gain_right.get("stage_output_peaks"),
            },
        })
        overall_score = (0.6 * min(left_score, right_score)) + (0.4 * ((left_score + right_score) / 2.0))
        left_xo_score = float(left_winner.get("xo_score", 0.0) or 0.0)
        right_xo_score = float(right_winner.get("xo_score", 0.0) or 0.0)
        left_timing_score = float(left_winner.get("timing_band_score", 0.0) or 0.0)
        right_timing_score = float(right_winner.get("timing_band_score", 0.0) or 0.0)
        left_low_guard_loss = float(left_winner.get("low_guard_loss_db", 0.0) or 0.0)
        right_low_guard_loss = float(right_winner.get("low_guard_loss_db", 0.0) or 0.0)
        left_low_guard_penalty = float(left_winner.get("low_guard_penalty", 0.0) or 0.0)
        right_low_guard_penalty = float(right_winner.get("low_guard_penalty", 0.0) or 0.0)
        overall_low_guard_loss = max(left_low_guard_loss, right_low_guard_loss)
        overall_low_guard_penalty = (
            0.6 * max(left_low_guard_penalty, right_low_guard_penalty)
            + 0.4 * ((left_low_guard_penalty + right_low_guard_penalty) / 2.0)
        )
        left_score_pct = round(left_score * 100.0, 1)
        right_score_pct = round(right_score * 100.0, 1)
        overall_score_pct = round(overall_score * 100.0, 1)
        job["status"] = "completed"
        job["message"] = (
            f"Applied 2.2 Stereo Bass: Left Sub {best_left:.2f} ms / "
            f"Right Sub {best_right:.2f} ms (overall {overall_score_pct:.1f} %)"
        )
        _log_auto_sub_timing_summary(job)

        # Build baseline and confirmation measurements for before/after graph display
        all_left_sweeps = list(left_results) + list(left_fine_results)
        all_right_sweeps = list(right_results) + list(right_fine_results)
        left_baseline = _auto_sub_result_for_delay(all_left_sweeps, original_left_alignment)
        right_baseline = _auto_sub_result_for_delay(all_right_sweeps, original_right_alignment)
        left_confirm = final_gain_left if accepted_step1_sides["left"] else _auto_sub_result_for_delay(all_left_sweeps, best_left)
        right_confirm = final_gain_right if accepted_step1_sides["right"] else _auto_sub_result_for_delay(all_right_sweeps, best_right)

        # One shared vertical offset from baseline L+R bass region so that
        # Before/After and L/R relative level differences are preserved.
        _stereo_offset_db = _auto_sub_shared_bass_offset(
            left_baseline.get("points") if left_baseline else [],
            right_baseline.get("points") if right_baseline else [],
        )

        def _stereo_measurement_from_lr(left_sweep, right_sweep, label, name, offset_db):
            traces = []
            base_id = uuid4().hex[:12]
            if left_sweep:
                pts = left_sweep.get("points") or []
                if isinstance(pts, list) and len(pts) >= 3:
                    points = [[float(p[0]), float(p[1]) - offset_db] for p in pts]
                    traces.append({"kind": "measured", "label": f"{label} L", "role": "left", "points": points})
            if right_sweep:
                pts = right_sweep.get("points") or []
                if isinstance(pts, list) and len(pts) >= 3:
                    points = [[float(p[0]), float(p[1]) - offset_db] for p in pts]
                    traces.append({"kind": "measured", "label": f"{label} R", "role": "right", "points": points})
            return {"id": f"autosub-{base_id}", "name": name, "traces": traces} if traces else None

        baseline_measurement = _stereo_measurement_from_lr(
            left_baseline, right_baseline, "Before",
            f"AutoSub 2.2S Baseline (L {original_left_alignment:.1f} / R {original_right_alignment:.1f} ms)",
            _stereo_offset_db,
        )
        confirmation_measurement = _stereo_measurement_from_lr(
            left_confirm, right_confirm, "After",
            f"AutoSub 2.2S Optimized (L {best_left:.1f} / R {best_right:.1f} ms)",
            _stereo_offset_db,
        )

        job["result"] = {
            "mode": OUTPUT_MODE_SUBWOOFER_22_STEREO,
            "original_sub1_alignment_ms": original_left_alignment,
            "original_sub2_alignment_ms": original_right_alignment,
            "suggested_sub1_alignment_ms": best_left,
            "suggested_sub2_alignment_ms": best_right,
            "applied_sub1_alignment_ms": best_left,
            "applied_sub2_alignment_ms": best_right,
            "applied": True,
            "auto_applied": True,
            "apply_decision": "applied_22_stereo_separate_lr",
            "candidate_ledger": candidate_ledger,
            "crossover_hz": fc,
            "confidence": "left_right_separate",
            "winner": {
                "name": _auto_sub_22_stereo_name(best_left, best_right),
                "score": round(overall_score, 4),
                "score_pct": overall_score_pct,
                "overall_score": round(overall_score, 4),
                "overall_score_pct": overall_score_pct,
                "xo_score": round((left_xo_score + right_xo_score) / 2.0, 4),
                "timing_band_score": round((left_timing_score + right_timing_score) / 2.0, 4),
                "low_guard_loss_db": round(overall_low_guard_loss, 2),
                "low_guard_penalty": round(overall_low_guard_penalty, 4),
                "final_score": round(overall_score, 4),
                "low_guard_loss_L_db": round(left_low_guard_loss, 2),
                "low_guard_loss_R_db": round(right_low_guard_loss, 2),
                "low_guard_penalty_L": round(left_low_guard_penalty, 4),
                "low_guard_penalty_R": round(right_low_guard_penalty, 4),
                "score_L_pct": left_score_pct,
                "score_R_pct": right_score_pct,
            },
            "left_score": round(left_score, 4),
            "right_score": round(right_score, 4),
            "overall_score": round(overall_score, 4),
            "xo_score": round((left_xo_score + right_xo_score) / 2.0, 4),
            "timing_band_score": round((left_timing_score + right_timing_score) / 2.0, 4),
            "low_guard_loss_db": round(overall_low_guard_loss, 2),
            "low_guard_penalty": round(overall_low_guard_penalty, 4),
            "final_score": round(overall_score, 4),
            "low_guard_loss_L_db": round(left_low_guard_loss, 2),
            "low_guard_loss_R_db": round(right_low_guard_loss, 2),
            "low_guard_penalty_L": round(left_low_guard_penalty, 4),
            "low_guard_penalty_R": round(right_low_guard_penalty, 4),
            "left_score_pct": left_score_pct,
            "right_score_pct": right_score_pct,
            "overall_score_pct": overall_score_pct,
            "accepted_winner": {
                "name": _auto_sub_22_stereo_name(best_left, best_right),
                "left_winner": left_winner,
                "right_winner": right_winner,
                "score": round(overall_score, 4),
                "score_pct": overall_score_pct,
            },
            "fine_accepted": bool(left_acceptance["fine_accepted"] or right_acceptance["fine_accepted"]),
            "reject_reason": {
                "left": left_acceptance["reject_reason"],
                "right": right_acceptance["reject_reason"],
            },
            "left_coarse_winner": left_coarse_winner,
            "left_fine_winner": left_fine_winner,
            "right_coarse_winner": right_coarse_winner,
            "right_fine_winner": right_fine_winner,
            "left_winner": left_winner,
            "right_winner": right_winner,
            "left_incumbent_winner": left_incumbent_winner,
            "right_incumbent_winner": right_incumbent_winner,
            "left_incumbent_score": left_acceptance["incumbent_score"],
            "right_incumbent_score": right_acceptance["incumbent_score"],
            "left_accepted_winner": left_winner,
            "right_accepted_winner": right_winner,
            "left_fine_accepted": left_acceptance["fine_accepted"],
            "right_fine_accepted": right_acceptance["fine_accepted"],
            "left_reject_reason": left_acceptance["reject_reason"],
            "right_reject_reason": right_acceptance["reject_reason"],
            "left_coarse_ranking": left_coarse_scoring["results"],
            "left_fine_ranking": left_fine_scoring["results"] if left_fine_scoring else [],
            "right_coarse_ranking": right_coarse_scoring["results"],
            "right_fine_ranking": right_fine_scoring["results"] if right_fine_scoring else [],
            "left_ranking": left_scoring["results"],
            "right_ranking": right_scoring["results"],
            "fine_scan": job["fine_scan"],
            "sweep_count": actual_sweep_total + 2,
            "candidate_count": actual_sweep_total,
            "left_candidate_count": len(left_scan_delays) + len(left_fine_delays),
            "right_candidate_count": len(right_scan_delays) + len(right_fine_delays),
            "left_coarse_candidate_count": len(left_scan_delays),
            "left_fine_candidate_count": len(left_fine_delays),
            "right_coarse_candidate_count": len(right_scan_delays),
            "right_fine_candidate_count": len(right_fine_delays),
            "valid_count": len(left_final_valid) + len(right_final_valid),
            "left_valid_count": len(left_final_valid),
            "right_valid_count": len(right_final_valid),
            "left_coarse_valid_count": len(left_valid),
            "left_fine_valid_count": len(left_fine_valid),
            "right_coarse_valid_count": len(right_valid),
            "right_fine_valid_count": len(right_fine_valid),
            "baseline_measurement": baseline_measurement,
            "confirmation_measurement": confirmation_measurement,
            **derived_delays,
        }
        logger.info(
            "Auto-sub 2.2 Stereo Bass optimize completed: fc=%sHz left %.2f->%.2fms right %.2f->%.2fms "
            "overall_score=%.1f%% score_L=%.1f%% score_R=%.1f%%",
            fc,
            original_left_alignment,
            best_left,
            original_right_alignment,
            best_right,
            overall_score_pct,
            left_score_pct,
            right_score_pct,
        )

    except Exception as exc:
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        logger.exception("Auto-sub 2.2 Stereo Bass optimize failed")
        job["status"] = "failed"
        job["message"] = f"Auto Sub Optimize 2.2 Stereo Bass failed: {exc}"
        job["error"] = {"detail": str(exc)}
        await _restore_original_config()

    finally:
        await _finish_auto_sub_worker(job, job_id)


async def _run_auto_sub_optimize(
    job_id: str,
    input_id: str,
    channel: str,
    mic_input_channel: str,
    reference_input_channel: str,
    calibration_ref: str,
    calibration_filename: str | None,
    calibration_bytes: bytes | None,
    scan_delays: list[float],
    fc: int,
    current_alignment: float,
    original_polarity: str,
    original_level: float,
    original_highpass: bool,
    original_config_snapshot: dict[str, Any],
    entry_epoch: int | None = None,
) -> None:
    from main import (
        measurement_sr_session,
        measurement_store,
        subwoofer_runtime,
    )
    from measurement_session import (
        MeasurementEntryInvalidated,
        _resolve_measurement_start_sample_rate,
    )
    global _auto_sub_lock
    from samplerate import _load_audio_output_mode, set_audio_output_mode

    job = _AUTO_SUB_JOBS.get(job_id)
    if not job:
        _auto_sub_lock.release()
        return

    async def _restore_original_config():
        """Restore subwoofer config from snapshot."""
        await _restore_auto_sub_original_config(original_config_snapshot)

    try:
        if measurement_sr_session is not None:
            try:
                await measurement_sr_session.register_auto_sub(job_id, entry_epoch=entry_epoch)
            except MeasurementEntryInvalidated:
                logger.info(
                    "AUTOSUB job=%s entry invalidated by measurement window close",
                    job_id,
                )
                job["status"] = "cancelled"
                job["message"] = "Auto Sub Optimize cancelled because the measurement window was closed."
                return
        if _auto_sub_cancel_requested(job):
            logger.info("AUTOSUB job=%s cancel observed (before sweeps)", job_id)
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        sweep_results: list[dict[str, Any]] = []
        total = len(scan_delays) * 2

        # AutoSub bass-focused sweep settings
        auto_sub_sweep_profile = _auto_sub_sweep_profile(fc)

        # Resolve sample rate once for all sweeps
        auto_sub_rate = _resolve_measurement_start_sample_rate()
        await _capture_auto_sub_main_references(
            job=job, fc=fc, input_id=input_id,
            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
            auto_sub_rate=auto_sub_rate, output_mode=OUTPUT_MODE_SUBWOOFER_21,
            original_config_snapshot=original_config_snapshot,
        )
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        coarse_total = len(scan_delays)
        coarse_sweep_total = coarse_total * 2
        total = coarse_sweep_total
        for idx, delay_ms in enumerate(scan_delays):
            sweep_results.append(
                await _measure_auto_sub_combined_candidate(
                    delay_ms=delay_ms,
                    job=job,
                    candidate_index=idx + 1,
                    total=coarse_total,
                    sweep_index_start=(idx * 2) + 1,
                    sweep_total=coarse_sweep_total,
                    stage="coarse",
                    fc=fc,
                    input_id=input_id,
                    mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel,
                    calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename,
                    calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile,
                    auto_sub_rate=auto_sub_rate,
                    original_level=original_level,
                    original_polarity=original_polarity,
                    original_highpass=original_highpass,
                )
            )
            if _auto_sub_cancel_requested(job):
                job["message"] = "Auto Sub Optimize cancelled."
                await _restore_original_config()
                return

            # Live: push baseline measurement to job for frontend polling display
            if round(float(delay_ms), 2) == round(float(current_alignment), 2):
                last_result = sweep_results[-1]
                if _auto_sub_has_points(last_result, "points_left") or _auto_sub_has_points(last_result, "points_right"):
                    job["baseline_measurement"] = _auto_sub_measurement_from_sweep(
                        last_result, "Before", f"AutoSub Baseline ({current_alignment:.1f} ms)"
                    )

        # Score candidates
        valid = [r for r in sweep_results if _auto_sub_has_points(r, "points_left") or _auto_sub_has_points(r, "points_right")]
        if not valid:
            job["status"] = "failed"
            job["message"] = "No valid sweep results to score"
            job["error"] = {"detail": "All sweeps failed or produced insufficient data"}
            await _restore_original_config()
            return

        step_ms = _auto_sub_step_ms(fc)
        coarse_scoring = _score_auto_sub_combined_candidates(
            sweep_results,
            crossover_hz=fc,
            low_guard_reference_delay_ms=current_alignment,
        )
        valid = list(coarse_scoring.get("scored_candidates") or valid)
        coarse_winner = coarse_scoring["winner"]
        coarse_runner_up = coarse_scoring.get("runner_up")
        fine_trigger_reasons = _auto_sub_fine_trigger_reasons(coarse_scoring, scan_delays)
        fine_delays: list[float] = []
        fine_results: list[dict[str, Any]] = []
        fine_valid: list[dict[str, Any]] = []
        fine_winner: dict[str, Any] | None = None
        fine_scoring: dict[str, Any] | None = None

        fine_scan: dict[str, Any] = {
            "enabled": bool(fine_trigger_reasons),
            "triggered": False,
            "reasons": fine_trigger_reasons,
            "step_ms": step_ms,
            "fine_step_ms": step_ms / 4.0,
            "candidates": [],
            "sweep_count": 0,
            "valid_count": 0,
            "status": "skipped" if not fine_trigger_reasons else "pending",
            "coarse_winner": coarse_winner,
            "coarse_runner_up": coarse_runner_up,
        }
        final_decision_pool = list(sweep_results)

        if fine_trigger_reasons:
            fine_delays = _auto_sub_fine_delay_candidates(coarse_winner, coarse_runner_up, step_ms, {round(float(delay), 2) for delay in scan_delays})
            fine_scan.update({
                "triggered": True,
                "candidates": fine_delays,
                "status": "running" if fine_delays else "skipped",
            })
            job["fine_scan"] = fine_scan
            if fine_delays:
                fine_candidate_total = len(fine_delays)
                fine_sweep_total = fine_candidate_total * 2
                total = coarse_sweep_total + fine_sweep_total
                reason_text = ", ".join(fine_trigger_reasons)
                job["stage"] = "fine_scan"
                job["message"] = f"Fine-Scan triggered ({reason_text}); {len(fine_delays)} candidates"
                job["progress"] = {
                    "current": coarse_sweep_total,
                    "total": total,
                    "sweep_current": coarse_sweep_total,
                    "sweep_total": total,
                    "candidate_current": 0,
                    "candidate_total": fine_candidate_total,
                    "stage": "fine",
                    "reason": reason_text,
                }
                if _auto_sub_cancel_requested(job):
                    job["message"] = "Auto Sub Optimize cancelled."
                    await _restore_original_config()
                    return
                for idx, delay_ms in enumerate(fine_delays):
                    fine_results.append(
                        await _measure_auto_sub_combined_candidate(
                            delay_ms=delay_ms,
                            job=job,
                            candidate_index=idx + 1,
                            total=fine_candidate_total,
                            sweep_index_start=coarse_sweep_total + (idx * 2) + 1,
                            sweep_total=total,
                            stage="fine",
                            fc=fc,
                            input_id=input_id,
                            mic_input_channel=mic_input_channel,
                            reference_input_channel=reference_input_channel,
                            calibration_ref=calibration_ref,
                            calibration_filename=calibration_filename,
                            calibration_bytes=calibration_bytes,
                            auto_sub_sweep_profile=auto_sub_sweep_profile,
                            auto_sub_rate=auto_sub_rate,
                            original_level=original_level,
                            original_polarity=original_polarity,
                            original_highpass=original_highpass,
                        )
                    )
                    if _auto_sub_cancel_requested(job):
                        job["message"] = "Auto Sub Optimize cancelled."
                        await _restore_original_config()
                        return

                fine_valid = [r for r in fine_results if _auto_sub_has_points(r, "points_left") or _auto_sub_has_points(r, "points_right")]
                if fine_valid:
                    fine_scoring = _score_auto_sub_combined_candidates(
                        fine_results,
                        crossover_hz=fc,
                        low_guard_reference_delay_ms=current_alignment,
                    )
                    fine_valid = list(fine_scoring.get("scored_candidates") or fine_valid)
                    fine_winner = fine_scoring["winner"]
                    fine_scan.update({
                        "status": "completed",
                        "candidate_count": len(fine_delays),
                        "sweep_count": len(fine_delays) * 2,
                        "valid_count": len(fine_valid),
                        "winner": fine_winner,
                        "runner_up": fine_scoring.get("runner_up"),
                        "results": fine_scoring["results"],
                    })
                    combined_valid = valid + fine_valid
                    final_decision_pool = list(combined_valid)
                    final_scoring = _score_auto_sub_combined_candidates(
                        combined_valid,
                        crossover_hz=fc,
                        low_guard_reference_delay_ms=current_alignment,
                    )
                    combined_valid = list(final_scoring.get("scored_candidates") or combined_valid)
                else:
                    fine_scan.update({
                        "status": "no_valid_results",
                        "candidate_count": len(fine_delays),
                        "sweep_count": len(fine_delays) * 2,
                        "valid_count": 0,
                        "winner": None,
                        "runner_up": None,
                        "results": fine_results,
                    })
                    combined_valid = valid
                    final_scoring = coarse_scoring
            else:
                fine_scan.update({
                    "status": "skipped",
                    "reason": "no fine candidates generated",
                })
                combined_valid = valid
                final_scoring = coarse_scoring
        else:
            combined_valid = valid
            final_scoring = coarse_scoring

        job["fine_scan"] = fine_scan
        _auto_sub_rank_results(final_scoring["results"])

        # Re-attach scan stage from original measured candidates (scoring creates fresh dicts)
        scan_by_delay: dict[float, str] = {}
        for result in valid:
            delay_key = round(float(result.get("delay_ms", 0.0)), 2)
            scan_by_delay[delay_key] = result.get("scan", "coarse")
        for result in fine_valid:
            delay_key = round(float(result.get("delay_ms", 0.0)), 2)
            scan_by_delay[delay_key] = result.get("scan", "fine")

        coarse_score_by_delay = {
            round(float(result.get("delay_ms", 0.0)), 2): result
            for result in coarse_scoring["results"]
        }
        for result in final_scoring["results"]:
            delay_key = round(float(result.get("delay_ms", 0.0)), 2)
            result["scan"] = scan_by_delay.get(delay_key, "coarse")
            if result["scan"] == "coarse":
                coarse_score = coarse_score_by_delay.get(delay_key)
                if coarse_score:
                    result["coarse_score"] = coarse_score.get("score")
                    result["coarse_score_pct"] = coarse_score.get("score_pct")
                    result["coarse_rank"] = coarse_score.get("rank")

        final_fine_winner = next(
            (result for result in final_scoring["results"] if result.get("scan") == "fine"),
            fine_winner,
        )
        if final_fine_winner is not None and fine_scan.get("status") == "completed":
            fine_scan["final_winner"] = final_fine_winner
        final_coarse_winner = _auto_sub_best_scan_result(final_scoring["results"], "coarse") or coarse_winner
        incumbent_winner = _auto_sub_result_for_delay(final_scoring["results"], current_alignment)
        acceptance = _auto_sub_select_accepted_winner(
            coarse_winner=final_coarse_winner,
            fine_winner=final_fine_winner if fine_scan.get("status") == "completed" else None,
            incumbent_winner=incumbent_winner,
        )
        fine_scan["coarse_winner"] = final_coarse_winner
        fine_scan["fine_winner"] = final_fine_winner
        fine_scan["incumbent_winner"] = incumbent_winner
        fine_scan["incumbent_score"] = acceptance["incumbent_score"]
        fine_scan["accepted_winner"] = acceptance["accepted_winner"]
        fine_scan["fine_accepted"] = acceptance["fine_accepted"]
        fine_scan["reject_reason"] = acceptance["reject_reason"]

        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        winner = acceptance["accepted_winner"]
        best_delay = winner["delay_ms"]
        confidence = str(final_scoring.get("confidence") or "uncertain")
        runner_up = final_scoring.get("runner_up")
        winner_score_pct = float(winner.get("score_pct", 0.0) or 0.0)
        runner_score_pct = float(runner_up.get("score_pct", 0.0) or 0.0) if runner_up else 0.0
        winner_margin_pct = winner_score_pct - runner_score_pct if runner_up else 100.0
        original_score_pct = None
        original_delay_key = round(float(current_alignment), 2)
        for scored_result in final_scoring.get("results", []):
            if round(float(scored_result.get("delay_ms", 0.0)), 2) == original_delay_key:
                original_score_pct = float(scored_result.get("score_pct", 0.0) or 0.0)
                break
        score_gain_pct = winner_score_pct - original_score_pct if original_score_pct is not None else None

        auto_apply = False
        apply_decision = "not_applied_uncertain_confidence"
        if incumbent_winner is not None and round(float(best_delay), 2) == round(float(current_alignment), 2):
            apply_decision = "not_applied_incumbent_better"
        elif confidence == "clear":
            auto_apply = True
            apply_decision = "applied_clear_confidence"
        elif confidence == "close":
            if winner_margin_pct < 2.0:
                apply_decision = "not_applied_close_margin_below_2pp"
            elif score_gain_pct is not None and score_gain_pct < 3.0:
                apply_decision = "not_applied_close_gain_below_3pp"
            else:
                auto_apply = True
                apply_decision = "applied_close_confidence"
        elif (
            confidence == "uncertain"
            and score_gain_pct is not None
            and score_gain_pct >= 10.0
            and winner_score_pct >= 70.0
        ):
            auto_apply = True
            apply_decision = "applied_uncertain_large_gain"

        apply_ok = False
        applied_delay = current_alignment
        if auto_apply:
            try:
                sub_config = {
                    "crossover_frequency_hz": fc,
                    "sub_alignment_ms": best_delay,
                    "sub_level_db": original_level,
                    "sub_polarity": original_polarity,
                    "main_highpass_enabled": original_highpass,
                }
                set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, sub_config)
                if subwoofer_runtime is not None:
                    config = SubwooferRuntimeConfig.from_overview(get_audio_output_overview())
                    await subwoofer_runtime.sync(config)
                await asyncio.sleep(0.3)
                verify = _load_audio_output_mode()
                if float(verify.get("subwoofer", {}).get("sub_alignment_ms", -999)) == best_delay:
                    apply_ok = True
                    applied_delay = best_delay
            except Exception as exc:
                logger.exception("Auto-sub: failed to apply winner delay %.2f ms", best_delay)
        else:
            await _restore_original_config()
            apply_ok = True

        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return

        if not apply_ok:
            job["status"] = "failed"
            job["message"] = f"Scoring succeeded but failed to apply winner delay {best_delay} ms"
            job["error"] = {"detail": "Winner apply failed — original config restored"}
            await _restore_original_config()
            return

        stored_winner = winner if auto_apply else (incumbent_winner or winner)
        gain_winner = _auto_sub_result_for_delay(list(sweep_results) + list(fine_results), stored_winner.get("delay_ms", current_alignment)) or {}
        final_polarity = original_polarity
        polarity_check: dict[str, Any] = {
            "incumbent": original_polarity, "selected": original_polarity,
            "accepted": False, "reason": "incumbent_protected",
        }
        if gain_winner and auto_apply:
            opposite = _auto_sub_opposite_polarity(original_polarity)
            alt = await _measure_auto_sub_combined_candidate(
                delay_ms=applied_delay, job=job, candidate_index=1, total=1,
                sweep_index_start=total + 1, sweep_total=total + 2, stage="polarity_check", fc=fc,
                input_id=input_id, mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                original_level=original_level, original_polarity=opposite,
                original_highpass=original_highpass,
            )
            try:
                polarity_scoring = _score_auto_sub_combined_candidates(
                    [dict(gain_winner, delay_ms=0.0), dict(alt, delay_ms=1.0)], crossover_hz=fc,
                    low_guard_reference_delay_ms=0.0,
                )
                scored_incumbent = _auto_sub_result_for_delay(polarity_scoring["results"], 0.0) or {}
                scored_alt = _auto_sub_result_for_delay(polarity_scoring["results"], 1.0) or {}
                polarity_check.update(_auto_sub_polarity_decision(scored_incumbent, scored_alt))
                polarity_check.update({"alternative": opposite, "incumbent_score": scored_incumbent.get("score"), "alternative_score": scored_alt.get("score")})
                if polarity_check["accepted"]:
                    final_polarity = opposite
                    gain_winner = alt
                    polarity_check["selected"] = opposite
                    local_delays = [
                        _auto_sub_clamped_delay(applied_delay + offset)
                        for offset in (-_auto_sub_step_ms(fc) / 2.0, -_auto_sub_step_ms(fc) / 4.0,
                                       _auto_sub_step_ms(fc) / 4.0, _auto_sub_step_ms(fc) / 2.0)
                    ]
                    polarity_fine: list[dict[str, Any]] = [alt]
                    for idx, delay in enumerate(local_delays):
                        polarity_fine.append(await _measure_auto_sub_combined_candidate(
                            delay_ms=delay, job=job, candidate_index=idx + 1, total=len(local_delays),
                            sweep_index_start=total + 3 + idx * 2, sweep_total=total + 2 + len(local_delays) * 2,
                            stage="polarity_fine", fc=fc, input_id=input_id,
                            mic_input_channel=mic_input_channel, reference_input_channel=reference_input_channel,
                            calibration_ref=calibration_ref, calibration_filename=calibration_filename,
                            calibration_bytes=calibration_bytes, auto_sub_sweep_profile=auto_sub_sweep_profile,
                            auto_sub_rate=auto_sub_rate, original_level=original_level,
                            original_polarity=opposite, original_highpass=original_highpass,
                        ))
                    fine_scored = _score_auto_sub_combined_candidates(polarity_fine, crossover_hz=fc)
                    fine_best = fine_scored["winner"]
                    fine_measured = _auto_sub_result_for_delay(polarity_fine, float(fine_best.get("delay_ms", applied_delay)))
                    if fine_measured:
                        applied_delay = float(fine_best["delay_ms"])
                        gain_winner = fine_measured
                    polarity_check["fine_scan"] = {"candidates": local_delays, "winner": fine_best}
            except Exception as exc:
                logger.warning("Auto-sub polarity check unavailable; restoring incumbent: %s", exc)
                final_polarity = original_polarity
                polarity_check.update({"accepted": False, "selected": original_polarity, "reason": "measurement_or_scoring_failed"})

            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, {
                "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                "sub_level_db": original_level, "sub_polarity": final_polarity,
                "main_highpass_enabled": original_highpass,
            })
            if subwoofer_runtime is not None:
                await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        job["polarity_check"] = polarity_check
        job["auto_gain"] = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_21,
            target_curve=job.get("target_curve"), anchor=job.get("main_target_anchor"),
            winner_curves={
                "left": gain_winner.get("calibrated_points_left") or [],
                "right": gain_winner.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        logger.info("AUTOSUB_GAIN mode=2.1 diagnostics=%s", json.dumps(job["auto_gain"], sort_keys=True))
        gain_deltas = _auto_sub_gain_deltas(job["auto_gain"], OUTPUT_MODE_SUBWOOFER_21, max_abs_db=6.0)
        applied_gain_delta = gain_deltas.get("left", 0.0)
        _auto_sub_gain_log_line("AUTOGAIN_INIT", {
            "mode": OUTPUT_MODE_SUBWOOFER_21, "xo_hz": fc,
            "target": (job.get("target_curve") or {}).get("label"),
            "anchor_hz": (job.get("main_target_anchor") or {}).get("usable_band_hz"),
            "target_offset_db": (job.get("main_target_anchor") or {}).get("target_vertical_offset_db"),
            "gain_before": original_level,
            "winner_delta_left": (job["auto_gain"].get("channels", {}).get("left") or {}).get("target_delta_db"),
            "winner_delta_right": (job["auto_gain"].get("channels", {}).get("right") or {}).get("target_delta_db"),
            "combined_delta_db": (job["auto_gain"].get("recommendation") or {}).get("raw_delta_db"),
            "first_step_db": applied_gain_delta,
        })
        gained_level = max(-24.0, min(12.0, original_level + applied_gain_delta))
        if gain_deltas:
            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, {
                "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                "sub_level_db": gained_level, "sub_polarity": final_polarity,
                "main_highpass_enabled": original_highpass,
            })
            if subwoofer_runtime is not None:
                await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        gain_after_sweep = await _measure_auto_sub_combined_candidate(
            delay_ms=applied_delay, job=job, candidate_index=1, total=1,
            sweep_index_start=total + 1, sweep_total=total + 2, stage="gain_after", fc=fc,
            input_id=input_id, mic_input_channel=mic_input_channel,
            reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
            calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
            auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
            original_level=gained_level, original_polarity=final_polarity,
            original_highpass=original_highpass, output_mode=OUTPUT_MODE_SUBWOOFER_21,
            original_config_snapshot=original_config_snapshot,
        )
        gain_after = _calculate_auto_sub_gain(
            mode=OUTPUT_MODE_SUBWOOFER_21, target_curve=job.get("target_curve"),
            anchor=job.get("main_target_anchor"), winner_curves={
                "left": gain_after_sweep.get("calibrated_points_left") or [],
                "right": gain_after_sweep.get("calibrated_points_right") or [],
            }, crossover_hz=fc,
        )
        gain_verdict = _auto_sub_gain_verdict(job["auto_gain"], gain_after, OUTPUT_MODE_SUBWOOFER_21)
        final_gain_deltas = gain_deltas if gain_verdict["accepted"] else {"left": 0.0, "right": 0.0}
        final_gain_level = gained_level if gain_verdict["accepted"] else original_level
        final_gain_sweep = gain_after_sweep if gain_verdict["accepted"] else gain_winner
        correction_deltas: dict[str, float] = {}
        correction_plan = None
        correction_after = None
        correction_verdict = None
        if not gain_verdict["accepted"]:
            set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, {
                "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                "sub_level_db": original_level, "sub_polarity": final_polarity,
                "main_highpass_enabled": original_highpass,
            })
            if subwoofer_runtime is not None:
                await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        else:
            correction_plan = _auto_sub_gain_response_correction(
                job["auto_gain"], gain_after, gain_deltas, OUTPUT_MODE_SUBWOOFER_21,
            )
            correction_deltas = correction_plan.get("deltas_db") or {}
            correction_delta = correction_deltas.get("left", 0.0)
            corrected_level = max(-24.0, min(12.0, gained_level + correction_delta))
            if not correction_plan.get("available"):
                correction_verdict = {
                    "accepted": False,
                    "reason": correction_plan.get("reason"),
                    "channels": {},
                    "step1_retained": True,
                }
            elif abs(correction_delta) > 0.0005:
                set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, {
                    "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                    "sub_level_db": corrected_level, "sub_polarity": final_polarity,
                    "main_highpass_enabled": original_highpass,
                })
                if subwoofer_runtime is not None:
                    await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
                correction_sweep = await _measure_auto_sub_combined_candidate(
                    delay_ms=applied_delay, job=job, candidate_index=1, total=1,
                    sweep_index_start=total + 3, sweep_total=total + 4, stage="gain_correction_after", fc=fc,
                    input_id=input_id, mic_input_channel=mic_input_channel,
                    reference_input_channel=reference_input_channel, calibration_ref=calibration_ref,
                    calibration_filename=calibration_filename, calibration_bytes=calibration_bytes,
                    auto_sub_sweep_profile=auto_sub_sweep_profile, auto_sub_rate=auto_sub_rate,
                    original_level=corrected_level, original_polarity=final_polarity,
                    original_highpass=original_highpass, output_mode=OUTPUT_MODE_SUBWOOFER_21,
                    original_config_snapshot=original_config_snapshot,
                )
                correction_after = _calculate_auto_sub_gain(
                    mode=OUTPUT_MODE_SUBWOOFER_21, target_curve=job.get("target_curve"),
                    anchor=job.get("main_target_anchor"), winner_curves={
                        "left": correction_sweep.get("calibrated_points_left") or [],
                        "right": correction_sweep.get("calibrated_points_right") or [],
                    }, crossover_hz=fc,
                )
                correction_verdict = _auto_sub_gain_verdict(gain_after, correction_after, OUTPUT_MODE_SUBWOOFER_21)
                if correction_verdict["accepted"]:
                    final_gain_deltas = {
                        "left": applied_gain_delta + correction_delta,
                        "right": applied_gain_delta + correction_delta,
                    }
                    final_gain_level = corrected_level
                    final_gain_sweep = correction_sweep
                else:
                    set_audio_output_mode(OUTPUT_MODE_SUBWOOFER_21, {
                        "crossover_frequency_hz": fc, "sub_alignment_ms": applied_delay,
                        "sub_level_db": gained_level, "sub_polarity": final_polarity,
                        "main_highpass_enabled": original_highpass,
                    })
                    if subwoofer_runtime is not None:
                        await subwoofer_runtime.sync(SubwooferRuntimeConfig.from_overview(get_audio_output_overview()))
        _auto_sub_gain_log_line("AUTOGAIN_FEEDBACK", {
            "gain_after_step1": gained_level,
            "score_before": _auto_sub_gain_log_score(job["auto_gain"]),
            "score_after_step1": _auto_sub_gain_log_score(gain_after),
            "response_per_db_left": (((correction_plan or {}).get("channels") or {}).get("left") or {}).get("response_change_per_db"),
            "response_per_db_right": (((correction_plan or {}).get("channels") or {}).get("right") or {}).get("response_change_per_db"),
            "remaining_error_left": (gain_after.get("channels", {}).get("left") or {}).get("target_delta_db"),
            "remaining_error_right": (gain_after.get("channels", {}).get("right") or {}).get("target_delta_db"),
            "raw_correction_db": ((correction_plan or {}).get("raw_deltas_db") or {}).get("left"),
            "applied_correction_db": ((correction_plan or {}).get("applied_deltas_db") or {}).get("left"),
            "correction_step_db": correction_deltas.get("left") if correction_deltas else None,
        })
        decision = "accepted_step2" if correction_verdict and correction_verdict.get("accepted") else (
            "accepted_step1" if gain_verdict.get("accepted") else "restored"
        )
        score_final_source = correction_after if decision == "accepted_step2" else (gain_after if decision == "accepted_step1" else job["auto_gain"])
        _auto_sub_gain_log_line("AUTOGAIN_RESULT", {
            "gain_final": final_gain_level, "score_final": _auto_sub_gain_log_score(score_final_source),
            "decision": decision, "reason": ((correction_verdict or gain_verdict) or {}).get("reason"),
            "delay_final": applied_delay,
        })
        job["auto_gain"].update({
            "applied": bool(gain_verdict["accepted"] and gain_deltas),
            "reverted": bool(gain_deltas and not gain_verdict["accepted"]),
            "verification": gain_after, "verification_verdict": gain_verdict,
            "response_correction": correction_plan,
            "correction_deltas_db": correction_deltas,
            "correction_verification": correction_after,
            "correction_verdict": correction_verdict,
            "final_deltas_db": final_gain_deltas,
            "original_level_db": original_level,
            "final_level_db": final_gain_level,
            "stage_output_peaks": (final_gain_sweep or {}).get("stage_output_peaks"),
        })
        stored_fine_accepted = bool(acceptance["fine_accepted"] and auto_apply)
        stored_reject_reason = acceptance["reject_reason"]
        if not auto_apply and stored_winner is incumbent_winner and round(float(best_delay), 2) != round(float(current_alignment), 2):
            stored_reject_reason = apply_decision
        fine_scan["accepted_winner"] = stored_winner
        fine_scan["fine_accepted"] = stored_fine_accepted
        fine_scan["reject_reason"] = stored_reject_reason
        candidate_ledger = (
            _auto_sub_candidate_ledger(
                sweep_results, final_scoring, mode="2.1", phase="coarse",
                roles={
                    "coarse_winner": final_coarse_winner,
                    "final_accepted_winner": stored_winner,
                },
                decision_pool=final_decision_pool,
                requested_incumbent={"delay_ms": current_alignment},
            )
            + _auto_sub_candidate_ledger(
                fine_results, final_scoring, mode="2.1", phase="fine",
                roles={"fine_winner": final_fine_winner, "final_accepted_winner": stored_winner},
                decision_pool=final_decision_pool,
                requested_incumbent={"delay_ms": current_alignment},
            )
        )

        job["status"] = "completed"
        job["message"] = (
            f"Applied: {best_delay} ms (score {winner['score_pct']:.0f} %)"
            if auto_apply
            else f"Suggested: {best_delay} ms (not applied: {confidence})"
        )
        _log_auto_sub_timing_summary(job)

        # Build baseline and confirmation measurements for before/after graph display
        baseline_measurement = None
        confirmation_measurement = None
        all_sweep_results = list(sweep_results) + list(fine_results)
        baseline_sweep = _auto_sub_result_for_delay(all_sweep_results, current_alignment)
        _offset_db = _auto_sub_shared_bass_offset(
            baseline_sweep.get("points_left") if baseline_sweep else [],
            baseline_sweep.get("points_right") if baseline_sweep else [],
        )
        if baseline_sweep and (_auto_sub_has_points(baseline_sweep, "points_left") or _auto_sub_has_points(baseline_sweep, "points_right")):
            baseline_measurement = _auto_sub_measurement_from_sweep(
                baseline_sweep, "Before", f"AutoSub Baseline ({current_alignment:.1f} ms)",
                offset_db=_offset_db,
            )
        confirm_delay = best_delay if auto_apply else current_alignment
        confirmation_sweep = final_gain_sweep if gain_verdict["accepted"] else _auto_sub_result_for_delay(all_sweep_results, confirm_delay)
        if confirmation_sweep and (_auto_sub_has_points(confirmation_sweep, "points_left") or _auto_sub_has_points(confirmation_sweep, "points_right")):
            confirm_label = "After" if auto_apply else "Current"
            confirmation_measurement = _auto_sub_measurement_from_sweep(
                confirmation_sweep, confirm_label, f"AutoSub {confirm_label} ({confirm_delay:.1f} ms)",
                offset_db=_offset_db,
            )

        job["result"] = {
            "original_alignment_ms": current_alignment,
            "suggested_alignment_ms": best_delay,
            "applied_alignment_ms": applied_delay,
            "applied_sub_alignment_ms": applied_delay,
            "applied": auto_apply,
            "auto_applied": auto_apply,
            "apply_decision": apply_decision,
            "winner_margin_pct": round(winner_margin_pct, 1),
            "score_gain_pct": round(score_gain_pct, 1) if score_gain_pct is not None else None,
            "original_score_pct": round(original_score_pct, 1) if original_score_pct is not None else None,
            "crossover_hz": fc,
            "confidence": confidence,
            "winner": winner,
            "coarse_winner": final_coarse_winner,
            "coarse_runner_up": coarse_runner_up,
            "fine_winner": final_fine_winner,
            "incumbent_winner": incumbent_winner,
            "incumbent_score": acceptance["incumbent_score"],
            "accepted_winner": stored_winner,
            "fine_accepted": stored_fine_accepted,
            "reject_reason": stored_reject_reason,
            "runner_up": final_scoring.get("runner_up"),
            "ranking": final_scoring["results"],
            "candidate_ledger": candidate_ledger,
            "sweep_count": total + 2,
            "candidate_count": coarse_total + len(fine_delays),
            "coarse_candidate_count": coarse_total,
            "fine_candidate_count": len(fine_delays),
            "coarse_sweep_count": coarse_sweep_total,
            "fine_sweep_count": len(fine_delays) * 2,
            "valid_count": len(combined_valid),
            "coarse_valid_count": len(valid),
            "fine_valid_count": len(fine_valid),
            "fine_scan": fine_scan,
            "baseline_measurement": baseline_measurement,
            "confirmation_measurement": confirmation_measurement,
        }

        logger.info(
            "Auto-sub optimize completed: fc=%sHz suggested=%.2fms applied=%s applied_delay=%.2fms combined_score=%.0f%% "
            "score_L=%.1f%% score_R=%.1f%% confidence=%s decision=%s fine_scan=%s",
            fc,
            best_delay,
            auto_apply,
            applied_delay,
            winner.get("score_pct", 0),
            winner.get("score_L_pct", 0) or 0,
            winner.get("score_R_pct", 0) or 0,
            confidence,
            apply_decision,
            fine_scan.get("status"),
        )

    except Exception as exc:
        if _auto_sub_cancel_requested(job):
            job["message"] = "Auto Sub Optimize cancelled."
            await _restore_original_config()
            return
        logger.exception("Auto-sub optimize failed")
        job["status"] = "failed"
        job["message"] = f"Auto Sub Optimize failed: {exc}"
        job["error"] = {"detail": str(exc)}
        await _restore_original_config()

    finally:
        await _finish_auto_sub_worker(job, job_id)
