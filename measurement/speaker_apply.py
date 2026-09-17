# SPDX-License-Identifier: AGPL-3.0-only
"""Guarded trial apply and combined acoustic confirmation for Speaker Align.

A proposal's predicted combined sum is not acoustic proof. This boundary
trial-stages the candidate on the runtime, re-acquires the speaker ways and
verifies the improvement from measured evidence — then always restores the
start rendering. Nothing is persisted here; the output-state revision never
moves. Commit, restore-after-commit, release, API and UI are later boundaries.

The runtime boundaries (``stage``/``restore``) are injected: ``stage`` must
either fully render the exact candidate it receives or leave the start
rendering untouched (restoring internally on failure); it must never mutate
its argument. The production wiring (plan compile, guarded rebuild, runtime
readback) belongs to the commit/integration slice, not here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any

from measurement.speaker_align import MAX_SUM_REGRESSION_DB, MIN_SUM_DB

# Ways must measure time-aligned after the trial apply. Arrival detection
# jitters a few samples; 0.25 ms (~12 samples at 48 kHz) is far below any
# meaningful correction yet far above detector noise. Pending qualification
# on real 2/3-way captures.
MAX_CONFIRMED_RESIDUAL_MS = 0.25


def _check_cancel(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise asyncio.CancelledError("Speaker Align trial was cancelled")


def _pair_key(check: dict[str, Any]) -> tuple:
    roles = check.get("roles")
    if not isinstance(roles, list) or len(roles) != 2 or any(not isinstance(role, str) for role in roles):
        raise ValueError("Speaker Align confirmation needs exactly paired overlap checks")
    return (roles[0], roles[1])


def verify_confirmation(
    baseline: dict[str, Any],
    confirmation: dict[str, Any],
    *,
    max_residual_ms: float = MAX_CONFIRMED_RESIDUAL_MS,
    max_regression_db: float = MAX_SUM_REGRESSION_DB,
) -> dict[str, Any]:
    """Decide from measured evidence whether the staged candidate confirmed.

    Both arguments are ``SpeakerAlignment.propose`` outputs: ``baseline``
    from the pre-apply acquisition, ``confirmation`` from the post-apply
    re-acquisition of the same frozen alignment. Malformed or rebased input
    raises; a merely unconvincing measurement returns ``confirmed: False``.
    """
    if type(max_residual_ms) not in (int, float) or not 0 < max_residual_ms < 1000:
        raise ValueError("Speaker Align confirmation residual must be a positive time in ms")
    if type(max_regression_db) not in (int, float) or not 0 <= max_regression_db < 100:
        raise ValueError("Speaker Align confirmation regression must be a non-negative level in dB")
    baseline_checks = baseline.get("overlap_checks")
    confirmation_checks = confirmation.get("overlap_checks")
    if not isinstance(baseline_checks, list) or not baseline_checks or not isinstance(confirmation_checks, list):
        raise ValueError("Speaker Align confirmation needs overlap checks on both sides")
    baseline_by_pair = {_pair_key(check): check for check in baseline_checks}
    confirmation_by_pair = {_pair_key(check): check for check in confirmation_checks}
    if set(baseline_by_pair) != set(confirmation_by_pair) or not baseline_by_pair:
        raise ValueError("Speaker Align confirmation pairs differ from the baseline proposal")
    if (baseline.get("start_revision") != confirmation.get("start_revision")
            or baseline.get("processing_fingerprint") != confirmation.get("processing_fingerprint")):
        raise ValueError("Speaker Align confirmation is rebased onto another start revision")
    residual_ms = max(abs(float(value)) for value in confirmation.get("added_delay_ms", {}).values())
    pairs = []
    for pair in sorted(baseline_by_pair):
        baseline_db = float(baseline_by_pair[pair]["after_sum_db"])
        confirmation_db = float(confirmation_by_pair[pair]["after_sum_db"])
        pairs.append({
            "roles": list(pair),
            "residual_within_pair_ms": residual_ms,
            "baseline_after_sum_db": baseline_db,
            "confirmation_after_sum_db": confirmation_db,
            "regression_db": baseline_db - confirmation_db,
        })
    worst_regression = max(row["regression_db"] for row in pairs)
    worst_sum = min(row["confirmation_after_sum_db"] for row in pairs)
    reasons = []
    if residual_ms > max_residual_ms:
        reasons.append(
            f"residual {residual_ms:.3f} ms exceeds {max_residual_ms:.3f} ms: ways still misaligned"
        )
    if worst_regression > max_regression_db:
        reasons.append(
            f"combined-sum regression {worst_regression:.2f} dB exceeds {max_regression_db:.2f} dB"
        )
    if worst_sum < MIN_SUM_DB:
        reasons.append(f"combined sum {worst_sum:.2f} dB below {MIN_SUM_DB:.1f} dB floor")
    return {
        "confirmed": not reasons,
        "reasons": reasons,
        "max_residual_ms": residual_ms,
        "max_regression_db": worst_regression,
        "min_confirmation_sum_db": worst_sum,
        "pairs": pairs,
    }


async def apply_and_confirm(
    *,
    stage: Callable[[dict[str, Any]], Awaitable[Any]],
    restore: Callable[[], Awaitable[None]],
    acquire: Callable[[], Awaitable[list[dict[str, Any]]]],
    alignment: Any,
    proposal: dict[str, Any],
    live_target: dict[str, Any],
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Trial-stage a proposal, confirm it acoustically, always restore.

    ``acquire`` returns post-apply captures in ``propose`` shape (the serial
    acquisition adapter produces exactly that). ``live_target`` must be freshly
    frozen for the proposal's area: the output state is never persisted here,
    so its revision still matches after staging. A failed measurement returns
    ``confirmed: False``; errors re-raise after restore. Cancellation restores
    through a shield before propagating.
    """
    for label, bound in (("stage", stage), ("restore", restore), ("acquire", acquire)):
        if not callable(bound):
            raise ValueError(f"Speaker Align trial requires a {label} boundary")
    if not isinstance(live_target, dict):
        raise ValueError("Speaker Align trial requires a frozen live target")
    if (live_target.get("revision") != proposal.get("start_revision")
            or live_target.get("processing_fingerprint") != proposal.get("processing_fingerprint")):
        raise ValueError(
            "Speaker Align live target is stale; revision and processing must "
            "match the proposal before trial staging"
        )
    _check_cancel(cancel_requested)
    receipt = await stage(deepcopy(proposal["candidate_state"]))
    try:
        _check_cancel(cancel_requested)
        captures = await acquire()
        _check_cancel(cancel_requested)
        confirmation = alignment.propose(captures, live_target=live_target)
        _check_cancel(cancel_requested)
        check = verify_confirmation(proposal, confirmation)
    except asyncio.CancelledError:
        await asyncio.shield(restore())
        raise
    except BaseException:
        await restore()
        raise
    await restore()
    return {
        "confirmed": check["confirmed"],
        "check": check,
        "confirmation": confirmation,
        "restored": True,
        "stage_receipt": receipt,
    }
