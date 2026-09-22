# SPDX-License-Identifier: AGPL-3.0-only

"""Shared AutoSub gain trial mechanics (2.1 and 2.2 mono)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GainTrialResult:
    """Bundled outcome of one gain trial with optional correction trial."""

    after_sweep: dict[str, Any]
    after_gain: dict[str, Any]
    verdict: dict[str, Any]
    correction_plan: dict[str, Any] | None
    correction_deltas: dict[str, float]
    correction_sweep: dict[str, Any] | None
    correction_gain: dict[str, Any] | None
    correction_verdict: dict[str, Any] | None
    retained_sweep: dict[str, Any]
    step1_accepted: bool
    step2_accepted: bool
    decision: str
    result_reason: str | None
    score_source: dict[str, Any]


def _unavailable_correction_verdict(reason: str | None) -> dict[str, Any]:
    """Correction verdict retaining step 1 when no correction is available."""
    return {
        "accepted": False,
        "reason": reason,
        "channels": {},
        "step1_retained": True,
    }


def _resolve_gain_trial_outcome(
    step1_accepted: bool,
    step1_verdict: dict[str, Any],
    correction_verdict: dict[str, Any] | None,
) -> tuple[str, str | None]:
    """Shared step decision and reason for a finished gain trial.

    Step 2 wins when its correction was accepted, otherwise a retained
    step 1 wins; the reason names a rejected step-2 correction explicitly
    so a retained step 1 never reads as rejected.
    """
    decision = "accepted_step2" if correction_verdict and correction_verdict.get("accepted") else (
        "accepted_step1" if step1_accepted else "restored"
    )
    reason = ((correction_verdict or step1_verdict) or {}).get("reason")
    if (
        decision == "accepted_step1" and correction_verdict
        and not correction_verdict.get("accepted")
        and "step1_retained" not in correction_verdict
    ):
        reason = f"Step-1 retained; step-2 correction rejected ({correction_verdict.get('reason')})"
    return decision, reason


async def _run_gain_trial(
    *,
    initial_gain: dict[str, Any],
    gain_deltas: dict[str, float],
    fallback_sweep: dict[str, Any],
    calc_gain_fn: Callable[[dict[str, Any]], dict[str, Any]],
    verdict_fn: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    correction_plan_fn: Callable[
        [dict[str, Any], dict[str, Any], dict[str, float]], dict[str, Any] | None
    ],
    capture_gain_after: Callable[[], Awaitable[dict[str, Any]]],
    restore_pre_gain: Callable[[], Awaitable[None]],
    capture_correction_after: Callable[[dict[str, float]], Awaitable[dict[str, Any]]],
    restore_step1: Callable[[], Awaitable[None]],
) -> GainTrialResult:
    """Run one gain trial with an optional response-correction trial.

    Captures the gained state, evaluates it, and restores the pre-gain
    state on rejection; otherwise plans a correction, captures it, and
    restores the step-1 state on correction rejection. Gain calculation,
    verdicts and correction plans resolve through the caller's functions
    so runner-level patching keeps working. Staging and measurement
    order match the former per-runner sequences; state representation
    (scalar levels versus snapshots) stays in the caller's closures.
    The caller keeps diagnostic formatting and result persistence.
    """
    after_sweep = await capture_gain_after()
    after_gain = calc_gain_fn(after_sweep)
    verdict = verdict_fn(initial_gain, after_gain)
    correction_plan: dict[str, Any] | None = None
    correction_deltas: dict[str, float] = {}
    correction_sweep: dict[str, Any] | None = None
    correction_gain: dict[str, Any] | None = None
    correction_verdict: dict[str, Any] | None = None
    if not verdict["accepted"]:
        await restore_pre_gain()
    else:
        correction_plan = correction_plan_fn(initial_gain, after_gain, gain_deltas)
        correction_deltas = correction_plan.get("deltas_db") or {}
        if not correction_plan.get("available"):
            correction_verdict = _unavailable_correction_verdict(correction_plan.get("reason"))
        elif abs(correction_deltas.get("left", 0.0)) > 0.0005:
            correction_sweep = await capture_correction_after(correction_deltas)
            correction_gain = calc_gain_fn(correction_sweep)
            correction_verdict = verdict_fn(after_gain, correction_gain)
            if not correction_verdict["accepted"]:
                await restore_step1()
    step1_accepted = bool(verdict.get("accepted"))
    step2_accepted = bool(correction_verdict and correction_verdict.get("accepted"))
    decision, result_reason = _resolve_gain_trial_outcome(step1_accepted, verdict, correction_verdict)
    score_source = correction_gain if decision == "accepted_step2" else (
        after_gain if decision == "accepted_step1" else initial_gain
    )
    retained_sweep = correction_sweep if step2_accepted else (
        after_sweep if step1_accepted else fallback_sweep
    )
    return GainTrialResult(
        after_sweep=after_sweep,
        after_gain=after_gain,
        verdict=verdict,
        correction_plan=correction_plan,
        correction_deltas=correction_deltas,
        correction_sweep=correction_sweep,
        correction_gain=correction_gain,
        correction_verdict=correction_verdict,
        retained_sweep=retained_sweep,
        step1_accepted=step1_accepted,
        step2_accepted=step2_accepted,
        decision=decision,
        result_reason=result_reason,
        score_source=score_source,
    )
