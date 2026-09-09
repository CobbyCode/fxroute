# SPDX-License-Identifier: AGPL-3.0-only

"""Sweep profiles, candidate construction, and delay/polarity math."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from audio.samplerate import (
    OUTPUT_MODE_SUBWOOFER_22_MODES,
    _load_audio_output_mode,
    get_audio_output_overview,
    set_audio_output_mode,
)
from dsp.runtime import BassManagementConfig

from .deps import _dsp_runtime

logger = logging.getLogger(__name__)

# Polarity acceptance thresholds: the cheap gate only decides whether
# refinement candidates are measured at all; the final flip decision is made
# from one shared normalization set (incumbent + all inverted candidates) and
# requires a clearly larger margin, because a polarity flip is a structural
# change that must not hinge on a two-candidate min-max vote.
_AUTO_SUB_MIN_POLARITY_GATE_GAIN: float = 0.03
_AUTO_SUB_MIN_POLARITY_ACCEPT_GAIN: float = 0.08


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

def _auto_sub_21_verify_restored(mode_state: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    """Verify a restored 2.1 subwoofer state matches the start snapshot."""
    try:
        if str(mode_state.get("mode") or "") != str(snapshot.get("mode") or ""):
            return False
        expected = snapshot.get("subwoofer") or {}
        actual = mode_state.get("subwoofer") or {}
        if not isinstance(actual, dict) or not isinstance(expected, dict):
            return False
        if abs(float(actual.get("sub_alignment_ms", -9999)) - _auto_sub_clamped_delay(float(expected.get("sub_alignment_ms", 0.0) or 0.0))) > 0.001:
            return False
        if abs(round(float(actual.get("sub_level_db", -9999)), 1) - round(float(expected.get("sub_level_db", 0.0) or 0.0), 1)) > 0.05:
            return False
        if str(actual.get("sub_polarity") or "normal") != str(expected.get("sub_polarity") or "normal"):
            return False
        if int(actual.get("crossover_frequency_hz", -9999)) != int(expected.get("crossover_frequency_hz", 80)):
            return False
        if bool(actual.get("main_highpass_enabled")) != bool(expected.get("main_highpass_enabled", True)):
            return False
        return True
    except (TypeError, ValueError):
        return False

async def _restore_auto_sub_original_config(original_config_snapshot: dict[str, Any]) -> bool:
    """Restore the start-of-run config and verify the live state matches.

    The restore runs through the same persist -> DSP sync -> settle ->
    read-back -> verify path the candidate configurations use, instead of a
    blind write: every AutoSub mode (2.1, 2.2 mono, 2.2 stereo) ends the run
    with the exact topology it began with. When the first read-back does not
    match, the restore is applied a second time and re-verified; only a
    persisting mismatch returns False so the runner can fail the job instead
    of leaving a silently different config active.
    """
    try:
        mode = str(original_config_snapshot.get("mode", "stereo") or "stereo")
        if mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
            sub1 = _auto_sub_22_sub(original_config_snapshot, "sub1")
            sub2 = _auto_sub_22_sub(original_config_snapshot, "sub2")
            global_config = _auto_sub_22_global_config(original_config_snapshot)
            subwoofers_config = _auto_sub_22_candidate_subwoofers(
                original_config_snapshot,
                sub1_alignment_ms=sub1["alignment_ms"],
                sub2_alignment_ms=sub2["alignment_ms"],
                active_subs=("sub1", "sub2"),
                sub1_polarity=sub1["polarity"],
                sub2_polarity=sub2["polarity"],
            )
            verify = lambda overview: _auto_sub_22_verify_subwoofers(  # noqa: E731
                overview, subwoofers_config, mode,
            )
        else:
            global_config = dict(original_config_snapshot.get("subwoofer") or {})
            subwoofers_config = None
            verify = lambda overview: _auto_sub_21_verify_restored(  # noqa: E731
                overview, original_config_snapshot,
            )
        for attempt in (1, 2):
            restored = await _auto_sub_apply_candidate(
                output_mode=mode,
                global_config=global_config,
                subwoofers_config=subwoofers_config,
                verify=verify,
                load_overview=_load_audio_output_mode,
            )
            if restored:
                return True
            if attempt == 1:
                logger.warning("Auto-sub: original config restore verification failed; re-applying once")
        return False
    except Exception:
        logger.exception("Auto-sub: failed to restore original config from snapshot")
        return False


async def _restore_original_config_or_fail_job(
    job: dict[str, Any],
    original_config_snapshot: dict[str, Any],
    message: str,
) -> bool:
    """Restore the start-of-run config and fail *job* when it cannot be verified.

    Shared body of the runners' ``_restore_original_config`` closures: the
    verified restore re-applies the original state and reads it back (with
    one re-apply on a transient mismatch); a persisting mismatch means the
    run would end with a different topology than it began with, so the job
    is marked failed instead. *message* names the mode for the user-visible
    job message and differs per runner by design.
    """
    restored = await _restore_auto_sub_original_config(original_config_snapshot)
    if not restored:
        prior_detail = str((job.get("error") or {}).get("detail") or "")
        restore_detail = "original config restore verification failed"
        job["status"] = "failed"
        job["message"] = message
        job["error"] = {
            "detail": f"{prior_detail}; {restore_detail}" if prior_detail else restore_detail,
        }
    return restored


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
    overview = await asyncio.to_thread(get_audio_output_overview)
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
        persisted_overview = await asyncio.to_thread(
            set_audio_output_mode, output_mode, global_config, subwoofers_config,
        )
        if _dsp_runtime() is not None:
            await _auto_sub_sync_dsp_runtime(
                output_mode=output_mode,
                persisted_overview=persisted_overview,
            )
        await asyncio.sleep(0.3)
        overview = await asyncio.to_thread(load_overview or get_audio_output_overview)
        return bool(verify(overview))
    except Exception:
        logger.exception("Auto-sub: candidate apply or verification failed")
        return False

def _auto_sub_step_ms(fc: int) -> float:
    return (1000.0 / float(fc)) / 16.0

def _auto_sub_clamped_delay(delay_ms: float) -> float:
    # "+ 0.0" normalizes -0.0 to 0.0 so scan windows never emit a negative
    # zero delay (a -0.0 winner delay silently broke the falsy `or` fallbacks
    # downstream and desynced the scored winner from the applied config).
    return round(max(-40.0, min(40.0, float(delay_ms))), 2) + 0.0

def _auto_sub_winner_delay_ms(winner: dict[str, Any] | None, fallback_ms: float) -> float:
    """Delay of a scoring winner with an explicit fallback for missing data.

    ``float(winner.get("delay_ms", fallback) or fallback)`` silently
    substitutes the fallback for a legitimate 0.00 ms winner because 0.0 is
    falsy: the real run auto-sub-799f3bd5d1ab scored a 0.0 ms right winner
    and applied the incumbent delay instead, desyncing the scored result
    from the applied configuration. An explicit None check keeps 0.0 as 0.0.
    """
    value = (winner or {}).get("delay_ms")
    if value is None:
        return _auto_sub_clamped_delay(float(fallback_ms))
    return _auto_sub_clamped_delay(float(value))

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

def _auto_sub_22_verify_subwoofers(
    mode_state: dict[str, Any], expected_subwoofers: dict[str, Any], expected_output_mode: str,
) -> bool:
    """Verify the complete persisted 2.2 subwoofer state."""
    actual_subwoofers = mode_state.get("subwoofers")
    if expected_output_mode not in OUTPUT_MODE_SUBWOOFER_22_MODES:
        return False
    if mode_state.get("mode") != expected_output_mode:
        return False
    if not isinstance(actual_subwoofers, dict):
        return False
    if not all(isinstance(actual_subwoofers.get(key), dict) for key in ("sub1", "sub2")):
        return False
    if not all(isinstance(expected_subwoofers.get(key), dict) for key in ("sub1", "sub2")):
        return False
    for sub_key in ("sub1", "sub2"):
        actual = _auto_sub_22_sub(mode_state, sub_key)
        expected = _auto_sub_22_sub({"subwoofers": expected_subwoofers}, sub_key)
        if abs(actual["alignment_ms"] - expected["alignment_ms"]) > 0.001:
            return False
        if abs(round(actual["level_db"], 1) - round(expected["level_db"], 1)) > 0.05:
            return False
        if actual["polarity"] != expected["polarity"]:
            return False
    return True

def _auto_sub_opposite_polarity(polarity: str) -> str:
    return "normal" if str(polarity).lower() == "invert" else "invert"

def _auto_sub_polarity_decision(
    incumbent: dict[str, Any], alternative: dict[str, Any], *,
    min_score_gain: float = _AUTO_SUB_MIN_POLARITY_GATE_GAIN,
) -> dict[str, Any]:
    """Cheap gate protecting the active polarity against unclear alternatives.

    This gate only decides whether inverted refinement candidates are worth
    measuring at all; the final flip decision must come from
    `_auto_sub_select_polarity_shared_winner` on the complete candidate set.
    """
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

def _auto_sub_coarse_winner_at_scan_edge(
    winner_delay_ms: float, scan_delays: list[float],
) -> str | None:
    """Return 'below'/'above' when the coarse winner sits on the scan edge.

    A winner on the edge means the coarse window could not see past it, so
    the fine scan should extend in that direction and the result should say
    so explicitly instead of clamping silently.
    """
    if not scan_delays:
        return None
    delays = sorted(float(delay) for delay in scan_delays)
    delay = float(winner_delay_ms)
    if abs(delay - delays[0]) <= 0.05:
        return "below"
    if abs(delay - delays[-1]) <= 0.05:
        return "above"
    return None

def _auto_sub_fine_delay_candidates(
    winner: dict[str, Any],
    runner_up: dict[str, Any] | None,
    step_ms: float,
    existing_delays: set[float],
    scan_delays: list[float] | None = None,
) -> list[float]:
    """Generate 4-6 fine delays around the coarse winner area.

    When the coarse winner sits on the scan edge, the coarse window could
    not look beyond it; the offsets then extend up to one full coarse step
    past that edge, and runner-up bridging is skipped so the edge direction
    always survives the candidate cap.
    """
    winner_delay = float(winner.get("delay_ms", 0.0))
    fine_step = step_ms / 4.0
    offsets: list[float] = []

    # Always sample winner +/- 0.25 and +/- 0.5 coarse step.
    offsets.extend([-2.0 * fine_step, -fine_step, fine_step, 2.0 * fine_step])

    edge = _auto_sub_coarse_winner_at_scan_edge(winner_delay, scan_delays or [])
    if edge:
        direction = -1.0 if edge == "below" else 1.0
        offsets.extend([direction * 3.0 * fine_step, direction * 4.0 * fine_step])
    elif runner_up is not None:
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
