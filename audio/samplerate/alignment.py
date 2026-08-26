# SPDX-License-Identifier: AGPL-3.0-only

"""Sink suspend/resume and force-rate reconciliation for samplerate alignment."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from audio import samplerate_orchestration

from .overview import (
    get_audio_output_overview,
    get_samplerate_status,
    playback_rate_aligned,
)
from audio.tool_env import c_locale_env
from .persistence import load_sample_rate_policy

logger = logging.getLogger(__name__)

SAMPLERATE_ALIGNMENT_TIMEOUT_MS = 900
SAMPLERATE_ALIGNMENT_POLL_INTERVAL_MS = 50
RATE_RENEGOTIATION_TRIGGER_WAIT_MS = 2500
SINK_SUSPEND_COOLDOWN_SECONDS = 3.0

_last_sink_suspend_at: float = 0.0
_last_sink_suspend_reason: str = ""


def pulse_suspend_sink_for_samplerate(output_key: str, reason: str) -> None:
    """Suspend and immediately resume a pactl sink to force rate re-negotiation."""
    if not output_key:
        return
    for suspend in ("1", "0"):
        completed = subprocess.run(
            ["pactl", "suspend-sink", output_key, suspend],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.5,
            env=c_locale_env(),
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise RuntimeError(stderr or f"pactl suspend-sink {output_key} {suspend} failed")
        if suspend == "1":
            time.sleep(0.3)
    logger.info("Measurement samplerate sink pulse completed: output=%s reason=%s", output_key, reason)

async def wait_for_samplerate_alignment(
    expected_rate: Optional[int],
    timeout_ms: int = SAMPLERATE_ALIGNMENT_TIMEOUT_MS,
) -> bool:
    """Poll the live samplerate status until the sink reaches the expected rate."""
    if not expected_rate or expected_rate <= 0:
        return False
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while time.monotonic() <= deadline:
        try:
            samplerate_status = await asyncio.to_thread(get_samplerate_status)
        except Exception:
            samplerate_status = {}
        sink_rate = samplerate_status.get("active_rate")
        if isinstance(sink_rate, int) and sink_rate == expected_rate:
            return True
        await asyncio.sleep(SAMPLERATE_ALIGNMENT_POLL_INTERVAL_MS / 1000)
    return False

def set_pipewire_force_rate(rate: int) -> None:
    """Write ``clock.force-rate`` through pw-metadata, bounded."""
    completed = subprocess.run(
        ["pw-metadata", "-n", "settings", "0", "clock.force-rate", str(rate)],
        capture_output=True,
        text=True,
        check=False,
        timeout=1.5,
        env=c_locale_env(),
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or f"pw-metadata clock.force-rate {rate} failed")

def get_current_pipewire_force_rate() -> Optional[int]:
    """Read the live force-rate; 0 means no force-rate is set."""
    try:
        status = get_samplerate_status()
    except Exception:
        return None
    force_rate = status.get("force_rate") if isinstance(status, dict) else None
    return force_rate if isinstance(force_rate, int) and force_rate > 0 else 0

async def suspend_resume_playback_sink(
    *,
    reason: str = "",
    output_key: str | None = None,
    force: bool = False,
) -> bool:
    """Central sink suspend/resume to force PipeWire rate re-negotiation.

    Args:
        reason: diagnostic label for logging
        output_key: pactl sink name; resolved from overview if None
        force: bypass cooldown

    Returns True if suspend/resume completed.
    """
    global _last_sink_suspend_at, _last_sink_suspend_reason
    now = time.monotonic()
    elapsed = now - _last_sink_suspend_at
    if not force and _last_sink_suspend_at > 0 and elapsed < SINK_SUSPEND_COOLDOWN_SECONDS:
        logger.warning(
            "Sink suspend/resume SKIPPED (cooldown %.1fs): reason=%s last_reason=%s",
            elapsed, reason, _last_sink_suspend_reason,
        )
        return False
    if output_key is None:
        overview = get_audio_output_overview()
        output_mode = overview.get("output_mode") or {}
        output_key = str(output_mode.get("effective_output_key") or "").strip()
    if not output_key:
        logger.warning("Sink suspend/resume SKIPPED: no output_key (reason=%s)", reason)
        return False
    logger.info("Sink suspend/resume START: reason=%s output_key=%s", reason, output_key)
    try:
        # pactl suspend pulses and the settle sleep run in a worker thread.
        await asyncio.to_thread(pulse_suspend_sink_for_samplerate, output_key, reason)
    except Exception as exc:
        logger.error("Sink suspend/resume FAILED: reason=%s output_key=%s error=%s", reason, output_key, exc)
        return False
    _last_sink_suspend_at = time.monotonic()
    _last_sink_suspend_reason = reason
    logger.info("Sink suspend/resume DONE: reason=%s output_key=%s", reason, output_key)
    return True

def clear_auto_policy_force_rate(
    expected_rate: int,
    *,
    app_policy: Mapping[str, Any] | None = None,
    status: Mapping[str, Any] | None = None,
    idle: bool = False,
) -> bool:
    """Clear the live force-rate when an auto policy holds the graph at its default rate.

    With an auto policy the force-rate is only needed to pin a target that
    differs from the graph default.  A leftover pin (e.g. after a
    fixed -> auto policy restore) otherwise keeps the samplerate status
    payload reporting ``mode=fixed`` while the persisted policy is auto.

    ``app_policy`` carries the in-flight policy of a policy-change transition
    (the persisted policy is still the old one at rate-application time); it
    defaults to the persisted policy.  ``status`` avoids a re-read when the
    caller already holds a fresh samplerate status (the ``default_rate`` field
    is stable across the reconciliation, so a pre-write snapshot is safe).

    ``idle`` marks an explicit stop/idle path: there is no active source, so
    any leftover pin is stale and is cleared regardless of the current rate
    (the default-rate guard below only applies to live playback, where a
    non-default pin is what keeps the sink on the source rate).
    """
    policy = dict(app_policy) if app_policy else load_sample_rate_policy()
    if policy.get("mode") != "auto":
        return False
    if status is None:
        try:
            status = get_samplerate_status()
        except Exception:
            return False
    if not idle:
        default_rate = status.get("default_rate")
        if not isinstance(default_rate, int) or expected_rate != default_rate:
            return False
    try:
        set_pipewire_force_rate(0)
    except Exception as exc:
        logger.warning(
            "Auto-policy force-rate clear failed: default_rate=%s error=%s",
            status.get("default_rate"), exc,
        )
        return False
    logger.info(
        "Auto-policy force-rate cleared at default rate=%s (expected=%s idle=%s)",
        status.get("default_rate"), expected_rate, idle,
    )
    return True

async def ensure_playback_samplerate_force(
    expected_rate: Optional[int],
    reason: str,
    *,
    allow_measurement_session: bool = False,
    policy: samplerate_orchestration.PlaybackRateReconcilePolicy = samplerate_orchestration.DEFAULT_POLICY,
    measurement_blocks_rate: Callable[[Optional[int]], Optional[int]] | None = None,
) -> bool:
    """Apply one bounded force-rate reconciliation policy for playback.

    ``measurement_blocks_rate`` is injected by the application owner: it
    returns the blocking measurement rate (or None) so an active measurement
    session defers playback rate changes.
    """
    if not isinstance(expected_rate, int) or expected_rate <= 0:
        return False
    if not allow_measurement_session and measurement_blocks_rate is not None:
        blocked_measurement_rate = measurement_blocks_rate(expected_rate)
        if blocked_measurement_rate is not None:
            logger.info(
                "Playback samplerate repair deferred to active measurement session: "
                "reason=%s playback_rate=%s measurement_rate=%s",
                reason,
                expected_rate,
                blocked_measurement_rate,
            )
            return False

    initial_status: dict = {}
    pulse_attempted = False
    pulse_succeeded = False

    async def read_status() -> dict:
        nonlocal initial_status
        try:
            initial_status = await asyncio.to_thread(get_samplerate_status)
        except Exception:
            initial_status = {}
        return initial_status

    async def write_force_rate(rate: int) -> None:
        await asyncio.to_thread(set_pipewire_force_rate, rate)
        logger.info(
            "Playback samplerate force-rate applied: reason=%s expected_rate=%s active_rate=%s previous_force_rate=%s",
            reason,
            expected_rate,
            initial_status.get("active_rate"),
            initial_status.get("force_rate"),
        )

    async def wait_for_alignment(rate: int, timeout_ms: int) -> bool:
        return await wait_for_samplerate_alignment(rate, timeout_ms=timeout_ms)

    async def pulse_sink(pulse_reason: str) -> bool:
        nonlocal pulse_attempted, pulse_succeeded
        pulse_attempted = True
        pulse_succeeded = await suspend_resume_playback_sink(
            reason=pulse_reason, force=True,
        )
        return pulse_succeeded

    aligned = await samplerate_orchestration.reconcile_playback_samplerate(
        expected_rate=expected_rate,
        reason=reason,
        policy=policy,
        read_status=read_status,
        write_force_rate=write_force_rate,
        wait_for_alignment=wait_for_alignment,
        pulse_sink=pulse_sink,
    )

    initial_active_rate = initial_status.get("active_rate")

    if policy is samplerate_orchestration.DEFAULT_POLICY and not aligned:
        if isinstance(initial_active_rate, int) and initial_active_rate != expected_rate:
            logger.info(
                "Radio samplerate sink suspend/resume SKIPPED: reason=%s "
                "(only for radio-start/restart paths)",
                reason,
            )
    elif policy is samplerate_orchestration.RADIO_POLICY and pulse_attempted:
        if aligned:
            logger.info(
                "Radio samplerate sink suspend/resume succeeded: reason=%s expected_rate=%s",
                reason, expected_rate,
            )
        else:
            logger.warning(
                "Radio samplerate sink suspend/resume did not change rate: reason=%s expected_rate=%s",
                reason, expected_rate,
            )
    return aligned

def rate_renegotiation_trigger_path(sample_rate: int) -> Path:
    return Path(tempfile.gettempdir()) / f"fxroute-rate-renegotiation-trigger-{sample_rate}.wav"

def ensure_rate_renegotiation_trigger_file(sample_rate: int) -> Path | None:
    """Generate (once) a short silent stream used to wake an idle hardware sink.

    A fully idle/suspended hardware sink ignores ``clock.force-rate`` writes
    and suspend/resume pulses; the only proven renegotiation trigger is a
    brief silent stream, after which the sink keeps the forced rate.
    """
    path = rate_renegotiation_trigger_path(sample_rate)
    if path.exists():
        return path
    try:
        generated = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=stereo",
                "-t", "0.8", "-c:a", "pcm_s16le", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        logger.warning("Rate renegotiation trigger generation failed: %s", exc)
        return None
    if generated.returncode != 0:
        logger.warning(
            "Rate renegotiation trigger generation failed: %s",
            (generated.stderr or "").strip(),
        )
        return None
    return path

async def trigger_idle_sink_renegotiation(sample_rate: int) -> bool:
    """Renegotiate an idle/suspended sink to the forced rate with a silent stream."""
    path = await asyncio.to_thread(ensure_rate_renegotiation_trigger_file, sample_rate)
    if path is None:
        return False
    try:
        subprocess.Popen(
            ["pw-play", "--volume=0", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.warning("Rate renegotiation trigger playback failed: %s", exc)
        return False
    logger.info(
        "Rate renegotiation trigger played: rate=%s (silent stream, volume 0)",
        sample_rate,
    )
    return await wait_for_samplerate_alignment(
        sample_rate, timeout_ms=RATE_RENEGOTIATION_TRIGGER_WAIT_MS
    )

async def reconcile_transition_sink_rate(
    target_rate: int,
    *,
    reason: str,
    measurement_blocks_rate: Callable[[Optional[int]], Optional[int]] | None = None,
) -> bool:
    """Re-establish the hardware sink rate before a transition stage commits.

    The effects/helper graph rebuild inside a transition can leave the sink
    suspended at the configured default rate while ``clock.force-rate`` already
    points at the target.  A suspended sink ignores force-rate writes and
    suspend/resume pulses; the bounded fallback below plays a short silent
    stream, the only proven renegotiation trigger on an idle graph.
    """
    try:
        status = dict(await asyncio.to_thread(get_samplerate_status))
    except Exception:
        status = {}
    if playback_rate_aligned(status, target_rate):
        return True
    aligned = await ensure_playback_samplerate_force(
        target_rate,
        reason=f"coordinator-{reason}",
        policy=samplerate_orchestration.RADIO_POLICY,
        measurement_blocks_rate=measurement_blocks_rate,
    )
    if not aligned:
        aligned = await trigger_idle_sink_renegotiation(target_rate)
    if not aligned:
        return False
    try:
        status = dict(await asyncio.to_thread(get_samplerate_status))
    except Exception:
        status = {}
    return bool(playback_rate_aligned(status, target_rate))

