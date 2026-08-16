# SPDX-License-Identifier: AGPL-3.0-only

"""Sweep profiles, candidate construction, and delay/polarity math."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from audio.samplerate import (
    OUTPUT_MODE_SUBWOOFER_22_MODES,
    get_audio_output_overview,
    set_audio_output_mode,
)
from dsp.runtime import BassManagementConfig

from .deps import _dsp_runtime

logger = logging.getLogger(__name__)


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
    try:
        from audio.samplerate import set_audio_output_mode
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
        if _dsp_runtime() is not None:
            await _dsp_runtime().sync(get_audio_output_overview())
    except Exception:
        logger.exception("Auto-sub: failed to restore original config from snapshot")

async def _auto_sub_sync_dsp_runtime(
    *,
    output_mode: str,
    persisted_overview: dict[str, Any],
) -> None:
    """Sync the native DSP runtime to the exact candidate/winner state.

    ``persisted_overview`` is the overview returned by the candidate
    ``set_audio_output_mode`` call, which persists the mode file synchronously
    and reads it back.  The live overview is re-read here and its derived
    bass configuration must match the persisted candidate on mode, sub
    alignments, levels, polarities, crossover and main high-pass.  A mismatch
    (for example a concurrent writer replacing the candidate with the
    incumbent state) raises instead of silently syncing the wrong topology,
    so every AutoSub sweep runs with exactly the gain/delay/polarity/
    crossover state the caller intends to evaluate.
    """
    if _dsp_runtime() is None:
        return
    overview = get_audio_output_overview()
    expected = BassManagementConfig.from_overview(persisted_overview)
    actual = BassManagementConfig.from_overview(overview)
    mismatches: list[str] = []
    if actual.output_mode != output_mode:
        mismatches.append(f"mode={actual.output_mode} (expected {output_mode})")
    if abs(actual.sub_alignment_ms - expected.sub_alignment_ms) > 0.05:
        mismatches.append(
            f"sub1 alignment={actual.sub_alignment_ms:.2f} ms "
            f"(expected {expected.sub_alignment_ms:.2f} ms)"
        )
    if actual.crossover_frequency_hz != expected.crossover_frequency_hz:
        mismatches.append(
            f"crossover={actual.crossover_frequency_hz} Hz "
            f"(expected {expected.crossover_frequency_hz} Hz)"
        )
    if bool(actual.main_highpass_enabled) != bool(expected.main_highpass_enabled):
        mismatches.append(
            f"main high-pass={actual.main_highpass_enabled} "
            f"(expected {expected.main_highpass_enabled})"
        )
    if abs(round(actual.sub_level_db, 1) - round(expected.sub_level_db, 1)) > 0.05:
        mismatches.append(
            f"sub1 level={actual.sub_level_db:.1f} dB "
            f"(expected {expected.sub_level_db:.1f} dB)"
        )
    if actual.sub_polarity != expected.sub_polarity:
        mismatches.append(
            f"sub1 polarity={actual.sub_polarity} (expected {expected.sub_polarity})"
        )
    if output_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
        if abs(actual.sub2_alignment_ms - expected.sub2_alignment_ms) > 0.05:
            mismatches.append(
                f"sub2 alignment={actual.sub2_alignment_ms:.2f} ms "
                f"(expected {expected.sub2_alignment_ms:.2f} ms)"
            )
        if abs(round(actual.sub2_level_db, 1) - round(expected.sub2_level_db, 1)) > 0.05:
            mismatches.append(
                f"sub2 level={actual.sub2_level_db:.1f} dB "
                f"(expected {expected.sub2_level_db:.1f} dB)"
            )
        if actual.sub2_polarity != expected.sub2_polarity:
            mismatches.append(
                f"sub2 polarity={actual.sub2_polarity} (expected {expected.sub2_polarity})"
            )
    if mismatches:
        raise RuntimeError(
            "AutoSub candidate state changed before DSP sync: " + "; ".join(mismatches)
        )
    await _dsp_runtime().sync(overview)

async def _auto_sub_apply_candidate(
    *,
    output_mode: str,
    global_config: dict[str, Any],
    subwoofers_config: dict[str, Any] | None,
    verify: Callable[[dict[str, Any]], bool],
    load_overview: Callable[[], dict[str, Any]] | None = None,
) -> bool:
    """Persist, live-sync, settle, and verify one mode-owned candidate."""
    try:
        persisted_overview = set_audio_output_mode(
            output_mode, global_config, subwoofers_config,
        )
        if _dsp_runtime() is not None:
            await _auto_sub_sync_dsp_runtime(
                output_mode=output_mode,
                persisted_overview=persisted_overview,
            )
        await asyncio.sleep(0.3)
        overview = (load_overview or get_audio_output_overview)()
        return bool(verify(overview))
    except Exception:
        logger.exception("Auto-sub: candidate apply or verification failed")
        return False

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

def _auto_sub_score_value(result: dict[str, Any] | None) -> float:
    if not result:
        return float("-inf")
    try:
        return float(result.get("final_score", result.get("score", 0.0)) or 0.0)
    except (TypeError, ValueError):
        return float("-inf")

