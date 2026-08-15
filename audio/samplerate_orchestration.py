"""I/O-independent playback sample-rate reconciliation primitives.

The policy constants describe the current FXRoute behavior. Callers inject all
system I/O so this module can be tested without importing the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping


StatusReader = Callable[[], Mapping[str, Any]]
ForceRateWriter = Callable[[int], None]
AlignmentWaiter = Callable[[int, int], Awaitable[bool]]
SinkPulse = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True)
class PlaybackRateReconcilePolicy:
    """Explicit policy for the bounded force/alignment/pulse sequence."""

    name: str
    initial_alignment_timeout_ms: int
    pulse_if_initial_active_rate_differs: bool
    pulse_if_initial_alignment_fails: bool
    post_pulse_alignment_timeout_ms: int | None


DEFAULT_POLICY = PlaybackRateReconcilePolicy(
    name="default",
    initial_alignment_timeout_ms=400,
    pulse_if_initial_active_rate_differs=False,
    pulse_if_initial_alignment_fails=False,
    post_pulse_alignment_timeout_ms=None,
)

RADIO_POLICY = PlaybackRateReconcilePolicy(
    name="radio-start-restart-transition",
    initial_alignment_timeout_ms=400,
    pulse_if_initial_active_rate_differs=True,
    pulse_if_initial_alignment_fails=False,
    post_pulse_alignment_timeout_ms=1200,
)

async def reconcile_playback_samplerate(
    *,
    expected_rate: int,
    reason: str,
    policy: PlaybackRateReconcilePolicy,
    read_status: StatusReader,
    write_force_rate: ForceRateWriter,
    wait_for_alignment: AlignmentWaiter,
    pulse_sink: SinkPulse,
) -> bool:
    """Apply one explicit bounded playback-rate reconciliation policy.

    The caller owns validation, measurement-session gates, and any runtime
    state bookkeeping. This function only coordinates injected callbacks and
    returns whether the sink aligned at the end of the policy.
    """
    status = read_status()
    active_rate = status.get("active_rate") if isinstance(status, Mapping) else None
    force_rate = status.get("force_rate") if isinstance(status, Mapping) else None

    if active_rate == expected_rate and force_rate == expected_rate:
        return True

    if force_rate != expected_rate:
        write_force_rate(expected_rate)

    aligned = await wait_for_alignment(
        expected_rate, policy.initial_alignment_timeout_ms,
    )
    if aligned:
        return True

    initial_active_rate_differs = (
        isinstance(active_rate, int) and active_rate != expected_rate
    )
    should_pulse = (
        policy.post_pulse_alignment_timeout_ms is not None
        and (
            policy.pulse_if_initial_alignment_fails
            or (
                policy.pulse_if_initial_active_rate_differs
                and initial_active_rate_differs
            )
        )
    )
    if not should_pulse:
        return False

    pulsed = await pulse_sink(reason)
    if not pulsed:
        return False

    return await wait_for_alignment(
        expected_rate, policy.post_pulse_alignment_timeout_ms,
    )
