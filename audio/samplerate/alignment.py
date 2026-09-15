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
from .constants import COMMAND_TIMEOUT_SECONDS

from .overview import (
    get_audio_output_overview,
    get_samplerate_status,
    playback_rate_aligned,
)
from .parsing import _parse_pactl_card_active_profile, _parse_pw_metadata_settings, _run_command
from audio.tool_env import c_locale_env
from .persistence import load_sample_rate_policy

logger = logging.getLogger(__name__)

SAMPLERATE_ALIGNMENT_TIMEOUT_MS = 900
SAMPLERATE_ALIGNMENT_POLL_INTERVAL_MS = 50
RATE_RENEGOTIATION_TRIGGER_WAIT_MS = 2500
CARD_RECYCLE_WAIT_MS = 4000
CARD_RECYCLE_SETTLE_SECONDS = 0.6
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
    """Read the live force-rate; 0 means no force-rate is set.

    Reads the ``clock.force-rate`` metadata entry directly instead of building
    the full samplerate status: the value comes from the same parse of the same
    command, but costs one subprocess instead of four.  Callers that only need
    the pin (idle watchers, measurement restore) must not pay for the sink and
    core reads they never look at.
    """
    try:
        metadata = _parse_pw_metadata_settings(_run_command(["pw-metadata", "-n", "settings", "0"]))
    except Exception:
        return None
    force_rate = metadata.get("force_rate")
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


def effective_output_key() -> str:
    """Return the selected playback output key from the live overview."""
    try:
        overview = get_audio_output_overview()
    except Exception:
        return ""
    output_mode = overview.get("output_mode") if isinstance(overview, Mapping) else None
    return str((output_mode or {}).get("effective_output_key") or "").strip()


def card_name_for_output(output_key: str) -> str | None:
    """Return the ALSA card backing an ALSA output key.

    PipeWire names an ALSA device's nodes ``alsa_output.<device>.<profile>``
    and ``alsa_input.<device>.<profile>`` and its card ``alsa_card.<device>``.
    """
    key = (output_key or "").strip()
    if not key.startswith("alsa_output."):
        return None
    device = key[len("alsa_output."):].rsplit(".", 1)[0]
    if not device:
        return None
    return f"alsa_card.{device}"


def active_card_profile(card_name: str) -> str | None:
    """Return the active profile of one card from ``pactl list cards``."""
    try:
        output = _run_command(["pactl", "list", "cards"])
    except Exception as exc:
        logger.warning("Card rate recycle profile lookup failed: %s", exc)
        return None
    return _parse_pactl_card_active_profile(output, card_name)


def recycle_card_profile(card_name: str, profile: str, reason: str) -> bool:
    """Cycle a card's profile off/on so its ALSA nodes reopen at the pin rate.

    A node that a measurement opened at the measurement rate keeps that rate
    while it stays alive: ``clock.force-rate`` writes, sink suspend/resume
    pulses, silent streams, capture streams at the target rate and DSP-helper
    rebuilds all leave the device there, and the next transition fails its
    target-rate stage.  Releasing the card's nodes is what lets the device
    reopen at the pinned rate, and the profile cycle is that release at the
    PulseAudio level.  ``off`` first, then the profile that was active, so the
    card ends in exactly the state it had.
    """
    for target_profile in ("off", profile):
        try:
            completed = subprocess.run(
                ["pactl", "set-card-profile", card_name, target_profile],
                capture_output=True,
                text=True,
                check=False,
                timeout=COMMAND_TIMEOUT_SECONDS,
                env=c_locale_env(),
            )
        except Exception as exc:
            logger.warning(
                "Card rate recycle failed: card=%s profile=%s reason=%s error=%s",
                card_name, target_profile, reason, exc,
            )
            return False
        if completed.returncode != 0:
            logger.warning(
                "Card rate recycle rejected: card=%s profile=%s reason=%s error=%s",
                card_name,
                target_profile,
                reason,
                (completed.stderr or "").strip(),
            )
            return False
        time.sleep(CARD_RECYCLE_SETTLE_SECONDS)
    logger.info(
        "Card rate recycle completed: card=%s profile=%s reason=%s",
        card_name, profile, reason,
    )
    return True


async def trigger_card_rate_recycle(sample_rate: int, *, reason: str = "") -> bool:
    """Recycle the selected output's card so its nodes reopen at the target rate.

    Last resort of the graph renegotiation ladder: it is disruptive for every
    other client of that card, so it only runs once the silent-stream trigger
    has already failed to move the sink.  Returns whether the sink aligned at
    ``sample_rate`` afterwards.
    """
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        return False
    output_key = await asyncio.to_thread(effective_output_key)
    card_name = card_name_for_output(output_key)
    if card_name is None:
        logger.info(
            "Card rate recycle skipped: selected output is not an ALSA card key=%s rate=%s",
            output_key, sample_rate,
        )
        return False
    profile = await asyncio.to_thread(active_card_profile, card_name)
    if not profile or profile == "off":
        logger.info(
            "Card rate recycle skipped: card=%s has no active profile to restore",
            card_name,
        )
        return False
    recycled = await asyncio.to_thread(
        recycle_card_profile, card_name, profile, reason or "graph-trigger"
    )
    if not recycled:
        return False
    return await wait_for_samplerate_alignment(
        sample_rate, timeout_ms=CARD_RECYCLE_WAIT_MS
    )


async def trigger_idle_sink_renegotiation(
    sample_rate: int, *, allow_card_recycle: bool = True
) -> bool:
    """Renegotiate the live graph onto the forced rate with a stream trigger.

    Two device states hold the rate: a suspended output node that ignores
    ``clock.force-rate`` (released by the silent sink stream below) and a card
    whose ALSA node still runs at the rate a finished measurement negotiated
    (only released by recycling the card's nodes).  The sink stream runs first
    because it is the non-disruptive path.

    ``allow_card_recycle`` is False for the measurement *entry* preflight: it
    runs with the measurement's own capture already open, so tearing the card
    down there would break the session that asked for the rate.
    """
    if await _trigger_silent_sink_stream(sample_rate):
        return True
    if not allow_card_recycle:
        return False
    return await trigger_card_rate_recycle(sample_rate)


async def _trigger_silent_sink_stream(sample_rate: int) -> bool:
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
        # Measurement entry preflight: the session's own capture is already
        # open, so the card must not be recycled underneath it.
        aligned = await trigger_idle_sink_renegotiation(
            target_rate, allow_card_recycle=False
        )
    if not aligned:
        return False
    try:
        status = dict(await asyncio.to_thread(get_samplerate_status))
    except Exception:
        status = {}
    return bool(playback_rate_aligned(status, target_rate))

