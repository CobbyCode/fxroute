# SPDX-License-Identifier: AGPL-3.0-only
"""Guarded trial apply and acoustic timing verification for Speaker Align.

A proposal's predicted timing is not acoustic proof. This boundary
trial-stages the candidate on the runtime, re-acquires the speaker ways and
verifies the measured arrival spread from evidence — then always restores the
start rendering. Nothing is persisted here; the output-state revision never
moves. Commit, restore-after-commit, release, API and UI are later boundaries.

The runtime boundaries (``stage``/``restore``) are injected. ``stage`` owns
an atomicity contract: it must either fully render the exact candidate it
receives or leave the start rendering untouched (restoring internally on
failure), so a stage failure runs no restore here. Once staging succeeded,
every later path restores (shielded against cancellation) before returning
or re-raising; a stage failure itself never triggers restore. ``stage`` must
never mutate its argument. The production wiring (plan compile, guarded
rebuild, runtime readback) belongs to the commit/integration slice, not here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
import math
from typing import Any

# Ways must measure time-aligned after the trial apply. Arrival detection
# jitters a few samples; 0.25 ms (~12 samples at 48 kHz) is far below any
# meaningful correction yet far above detector noise. Pending qualification
# on real 2/3-way captures.
MAX_CONFIRMED_RESIDUAL_MS = 0.25


def _check_cancel(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise asyncio.CancelledError("Speaker Align trial was cancelled")


def _finite(value: object, label: str) -> float:
    # bool is an int subclass but never a measurement; numpy floats pass
    # isinstance and are real arithmetic, unlike Decimal/str which stay out.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Speaker Align {label} must be finite")
    return float(value)


def _require_identity(document: dict[str, Any], label: str) -> tuple:
    """Require a present, non-null start revision and processing fingerprint.

    A missing key must never compare equal to another missing key: absent
    identity fails closed instead of verifying as "same revision".
    """
    revision = document.get("start_revision")
    fingerprint = document.get("processing_fingerprint")
    if type(revision) is not int:
        raise ValueError(f"Speaker Align {label} carries no start revision")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError(f"Speaker Align {label} carries no processing fingerprint")
    return revision, fingerprint


def verify_confirmation(
    baseline: dict[str, Any],
    confirmation: dict[str, Any],
    *,
    max_residual_ms: float = MAX_CONFIRMED_RESIDUAL_MS,
    max_gain_spread_db: float | None = None,
) -> dict[str, Any]:
    """Decide from measured evidence whether the staged candidate confirmed.

    ``baseline`` is the planning ``SpeakerAlignment.propose`` output, whose
    arrivals were measured per way, and ``confirmation`` is the post-apply
    document of one shared per-side verification take. That take's arrivals
    are band-limited way arrivals on a single capture time base, so the
    residual is a real acoustic low/high offset instead of the per-way
    microphone-minus-own-reference difference a staged way delay cancels out
    of. The residual is judged from the confirmation take alone; the
    baseline's spread stays as the before-value. Time residual and gain spread
    are verified jointly: ways must measure time-aligned and their passband
    levels must agree within the gain tolerance. Malformed or rebased input
    raises ``ValueError``; a merely unconvincing measurement returns
    ``confirmed: False``.
    """
    from measurement.alignment_backend import MAX_VERIFIED_GAIN_SPREAD_DB
    from measurement.speaker_verification import MIN_WAY_ISOLATION_DB
    if max_gain_spread_db is None:
        max_gain_spread_db = MAX_VERIFIED_GAIN_SPREAD_DB
    if not isinstance(baseline, dict) or not isinstance(confirmation, dict):
        raise ValueError("Speaker Align confirmation needs proposal outputs on both sides")
    if type(max_residual_ms) not in (int, float) or not 0 < max_residual_ms < 1000:
        raise ValueError("Speaker Align confirmation residual must be a positive time in ms")
    if type(max_gain_spread_db) not in (int, float) or not 0 < max_gain_spread_db < 24:
        raise ValueError("Speaker Align confirmation gain spread must be a positive dB tolerance")
    before = baseline.get("arrival_ms")
    after = confirmation.get("arrival_ms")
    if (not isinstance(before, dict) or not isinstance(after, dict)
            or len(before) not in (2, 3, 4) or set(before) != set(after)):
        raise ValueError("Speaker Align confirmation requires the same measured ways")
    if _require_identity(baseline, "baseline") != _require_identity(confirmation, "confirmation"):
        raise ValueError("Speaker Align confirmation is rebased onto another start revision")
    before = {role: _finite(value, "baseline arrival") for role, value in before.items()}
    after = {role: _finite(after[role], "confirmation arrival") for role in before}
    residual_ms = max(after.values()) - min(after.values())
    roles = list(before)
    pairs = [{"roles": [low, high], "residual_within_pair_ms": abs(after[low] - after[high])}
             for low, high in zip(roles, roles[1:])]
    reasons = []
    if residual_ms > max_residual_ms:
        reasons.append(
            f"residual {residual_ms:.3f} ms exceeds {max_residual_ms:.3f} ms: ways still misaligned"
        )
    # A shared take separates the side's ways by their own bands. When one way
    # is so far below its neighbour that its band is really the neighbour's
    # leak, its arrival is not its own and the residual would read as aligned
    # whatever the rendering does: report that instead of confirming.
    isolation = confirmation.get("way_isolation_db")
    weakest_isolation_db: float | None = None
    if isinstance(isolation, dict) and set(isolation) == set(after):
        margins = [_finite(value, "way isolation") for value in isolation.values()
                   if value is not None]
        if margins:
            weakest_isolation_db = min(margins)
            if weakest_isolation_db < MIN_WAY_ISOLATION_DB:
                reasons.append(
                    f"way isolation {weakest_isolation_db:.3f} dB is below "
                    f"{MIN_WAY_ISOLATION_DB:.3f} dB: the shared take cannot "
                    "separate the ways"
                )
    elif isolation is not None:
        raise ValueError("Speaker Align confirmation isolation evidence names different ways")
    before_levels = baseline.get("way_levels_db") or {}
    after_levels = confirmation.get("after_way_levels_db", confirmation.get("way_levels_db")) or {}
    gain_spread_db: float | None = None
    before_gain_spread_db: float | None = None
    if isinstance(before_levels, dict) and isinstance(after_levels, dict) and set(before_levels) == set(after) and set(after_levels) == set(after):
        try:
            before_values = {role: _finite(before_levels[role], "baseline level") for role in after}
            after_values = {role: _finite(after_levels[role], "confirmation level") for role in after}
            # Legacy shape without response points reports all-zero levels;
            # time alone decides then, gain trivially holds.
            has_gain_evidence = any(abs(value) > 1e-9 for value in list(before_values.values()) + list(after_values.values()))
            if has_gain_evidence:
                before_gain_spread_db = max(before_values.values()) - min(before_values.values())
                gain_spread_db = max(after_values.values()) - min(after_values.values())
                if gain_spread_db > max_gain_spread_db:
                    reasons.append(
                        f"gain spread {gain_spread_db:.3f} dB exceeds {max_gain_spread_db:.3f} dB: ways differ in level"
                    )
        except ValueError:
            raise
    result = {
        "confirmed": not reasons,
        "reasons": reasons,
        "max_residual_ms": residual_ms,
        "before_spread_ms": max(before.values()) - min(before.values()),
        "after_arrival_ms": after,
        "tolerance_ms": max_residual_ms,
        "pairs": pairs,
        "gain_spread_db": gain_spread_db,
        "before_gain_spread_db": before_gain_spread_db,
        "gain_tolerance_db": max_gain_spread_db,
        "way_isolation_db": dict(isolation) if isinstance(isolation, dict) else None,
        "isolation_margin_db": weakest_isolation_db,
        "isolation_tolerance_db": MIN_WAY_ISOLATION_DB,
    }
    if isinstance(after_levels, dict) and after_levels:
        result["after_way_levels_db"] = {role: float(after_levels[role]) for role in after_levels}
    return result


async def apply_and_confirm(
    *,
    stage: Callable[[dict[str, Any]], Awaitable[Any]],
    restore: Callable[[], Awaitable[None]],
    confirm: Callable[[], Awaitable[dict[str, Any]]],
    proposal: dict[str, Any],
    live_target: dict[str, Any],
    cancel_requested: Callable[[], bool] | None = None,
    max_residual_ms: float | None = None,
    max_gain_spread_db: float | None = None,
) -> dict[str, Any]:
    """Trial-stage a proposal, confirm it acoustically, always restore.

    ``confirm`` measures the staged rendering and returns the post-apply
    verification document for the same frozen alignment (one shared take per
    speaker side, so the residual cannot cancel itself out the way per-way
    references do). ``live_target`` must be freshly frozen for the proposal's
    area: the output state is never persisted here, so its revision still
    matches after staging. ``max_residual_ms`` overrides the confirmation gate
    for hardware qualification; defaults to the module gate. A failed
    measurement returns ``confirmed: False``; errors re-raise after restore,
    except a failing restore itself surfaces loudly (chained onto the original
    error as context). A stage failure runs no restore (the stage boundary owns
    atomicity); every later path restores shielded against cancellation before
    returning or raising.
    """
    for label, bound in (("stage", stage), ("restore", restore), ("confirm", confirm)):
        if not callable(bound):
            raise ValueError(f"Speaker Align trial requires a {label} boundary")
    if not isinstance(live_target, dict):
        raise ValueError("Speaker Align trial requires a frozen live target")
    if not isinstance(proposal, dict):
        raise ValueError("Speaker Align trial requires a proposal")
    candidate_state = proposal.get("candidate_state")
    if not isinstance(candidate_state, dict):
        raise ValueError("Speaker Align trial proposal carries no candidate state")
    live_identity = _require_identity(
        {"start_revision": live_target.get("revision"),
         "processing_fingerprint": live_target.get("processing_fingerprint")},
        "live target")
    if live_identity != _require_identity(proposal, "proposal"):
        raise ValueError(
            "Speaker Align live target is stale; revision and processing must "
            "match the proposal before trial staging"
        )
    verify_options = {}
    if max_residual_ms is not None:
        verify_options["max_residual_ms"] = max_residual_ms
    if max_gain_spread_db is not None:
        verify_options["max_gain_spread_db"] = max_gain_spread_db
    _check_cancel(cancel_requested)
    receipt = await stage(deepcopy(candidate_state))
    try:
        _check_cancel(cancel_requested)
        confirmation = await confirm()
        _check_cancel(cancel_requested)
        check = verify_confirmation(proposal, confirmation, **verify_options)
    except asyncio.CancelledError:
        await asyncio.shield(restore())
        raise
    except BaseException:
        await asyncio.shield(restore())
        raise
    await asyncio.shield(restore())
    return {
        "confirmed": check["confirmed"],
        "check": check,
        "confirmation": confirmation,
        "restored": True,
        "stage_receipt": receipt,
    }
