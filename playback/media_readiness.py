# SPDX-License-Identifier: AGPL-3.0-only

"""PipeWire sink-input observation plus MPV/media readiness waits.

Extracted verbatim from main.py (PipeWire-/Sink-Input-/Media-Readiness block).
Behavior, timing, timeouts, logging and error messages are identical; main.py
keeps only the integration wiring (runtime/player/epoch/drain bindings).

No imports from main: stateless observation/release logic uses only
audio.sink_inputs / audio.pw_link plus stdlib; player- and radio-dependent
waits receive their runtime dependencies explicitly (get_player/drain_worker/
get_epoch) from the composition root.
"""

from __future__ import annotations

import asyncio
import logging
import math
import subprocess
import time
from typing import Any, Callable, Optional

import audio.sink_inputs as sink_inputs
from audio import pw_link
from playback.spotify_watch import SPOTIFY_SINK_INPUT_RATE_STABILITY_POLLS

logger = logging.getLogger(__name__)

PIPEWIRE_HANDOFF_RELEASE_TIMEOUT_MS = 1800
# qbzd fully disconnects its ALSA stream on pause instead of corking it like
# Spotify Desktop, and that release takes ~2.2 s (live-measured). It therefore
# gets its own, longer bounded release budget so an MPV handoff does not fail
# while the previous Qobuz stream is still draining.
PIPEWIRE_QOBUZ_RELEASE_TIMEOUT_MS = 4000
PIPEWIRE_HANDOFF_POLL_INTERVAL_MS = 50
SPOTIFY_SINK_INPUT_RATE_TIMEOUT_MS = 1800
PEAK_MONITOR_RATE_MATCH_TIMEOUT_MS = 900
RADIO_POST_LOAD_RATE_TIMEOUT_MS = 3000
RADIO_POST_LOAD_RATE_STABILITY_POLLS = 3
# Bounded read-only budget for the MPV stream's PipeWire output ports to
# appear after a staged cold radio loadfile: mpv publishes mpv:output_FL/FR
# only once the network stream actually opened (observed ~4 s cold start).
# The MPV->DSP ingress link repair must never run while the ports are
# absent; this is source-startup readiness, not a fixed sleep.
RADIO_SOURCE_PORT_READINESS_TIMEOUT_MS = 4500


def list_sink_inputs() -> list[dict]:
    """Thin wrapper: pactl sink-input parsing lives in sink_inputs."""
    return sink_inputs.list_sink_inputs()


def list_mpv_sink_inputs() -> list[dict]:
    return [
        entry
        for entry in list_sink_inputs()
        if (entry.get("properties") or {}).get("application.name") == "mpv"
        or (entry.get("properties") or {}).get("application.id") == "mpv"
        or (entry.get("properties") or {}).get("node.name") == "mpv"
    ]


def list_spotify_sink_inputs() -> list[dict]:
    def matches(value: str) -> bool:
        if value == "spotify":
            return True
        if value == "spotifyd" or value.startswith("spotifyd."):
            return True
        return False

    return [
        entry
        for entry in list_sink_inputs()
        if matches(str((entry.get("properties") or {}).get("application.name") or "").lower())
        or matches(str((entry.get("properties") or {}).get("application.id") or "").lower())
        or matches(str((entry.get("properties") or {}).get("node.name") or "").lower())
        or matches(str((entry.get("properties") or {}).get("application.process.binary") or "").lower())
        or (entry.get("properties") or {}).get("media.name") == "Spotify"
    ]


def list_qobuz_sink_inputs() -> list[dict]:
    """Return PipeWire sink inputs produced by the qbzd renderer."""
    result: list[dict] = []
    for entry in list_sink_inputs():
        properties = entry.get("properties") or {}
        haystack = " ".join(
            str(properties.get(key) or "")
            for key in ("application.name", "application.id", "node.name", "media.name")
        ).lower()
        if "qobuz" in haystack or "qbzd" in haystack:
            result.append(entry)
    return result


def sink_input_observation(
    entries: list[dict],
    *,
    expected_rate: int | None = None,
    preferred_identity: object | None = None,
) -> tuple[object, int] | None:
    """Select one active sink input and retain an identity for stability checks."""
    candidates: list[tuple[object, int]] = []
    for entry in entries:
        corked = entry.get("corked")
        if isinstance(corked, str):
            corked = corked.strip().lower() in {"1", "true", "yes", "on"}
        if corked:
            # A corked input is an old/paused PipeWire stream.  It must not
            # validate a new Playing entry or hide a newly created active
            # input with a different rate.
            continue
        rate = entry.get("sample_rate")
        if isinstance(rate, int) and rate > 0:
            properties = entry.get("properties") or {}
            identity: object = entry.get("id")
            if identity is None:
                identity = entry.get("index")
            if identity is None:
                identity = (
                    properties.get("node.name"),
                    properties.get("application.name") or properties.get("application.id"),
                    properties.get("media.name"),
                )
            candidates.append((identity, rate))
    if not candidates:
        return None

    selected_identity, selected_rate = candidates[0]
    preferred = next(
        (
            candidate
            for candidate in candidates
            if preferred_identity is not None and candidate[0] == preferred_identity
        ),
        None,
    )
    expected = next(
        (
            candidate
            for candidate in candidates
            if isinstance(expected_rate, int)
            and expected_rate > 0
            and candidate[1] == expected_rate
        ),
        None,
    )
    if preferred is not None and (expected_rate is None or preferred[1] == expected_rate):
        selected_identity, selected_rate = preferred
    elif expected is not None:
        # A stale preferred input must not mask a newly appeared input that
        # already has the rate required by the Coordinator commit contract.
        selected_identity, selected_rate = expected
    elif preferred is not None:
        selected_identity, selected_rate = preferred
    return selected_identity, selected_rate


def spotify_sink_input_observation(
    entries: list[dict],
    *,
    expected_rate: int | None = None,
    preferred_identity: object | None = None,
) -> tuple[object, int] | None:
    """Select one active Spotify sink input (identity + rate)."""
    return sink_input_observation(
        entries,
        expected_rate=expected_rate,
        preferred_identity=preferred_identity,
    )


def qobuz_sink_input_observation(
    entries: list[dict],
    *,
    expected_rate: int | None = None,
    preferred_identity: object | None = None,
) -> tuple[object, int] | None:
    """Select one active qbzd sink input (identity + rate)."""
    return sink_input_observation(
        entries,
        expected_rate=expected_rate,
        preferred_identity=preferred_identity,
    )


def active_unmuted_sink_inputs(entries: list[dict]) -> list[dict]:
    return sink_inputs.active_unmuted_sink_inputs(entries)


async def wait_for_sink_input_release(list_fn, timeout_ms: int) -> bool:
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while time.monotonic() <= deadline:
        # The listing spawns pactl; keep that subprocess off the event loop.
        if not await asyncio.to_thread(list_fn):
            return True
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
    return not await asyncio.to_thread(list_fn)


async def wait_for_pipewire_mpv_release(timeout_ms: int = PIPEWIRE_HANDOFF_RELEASE_TIMEOUT_MS) -> bool:
    return await wait_for_sink_input_release(list_mpv_sink_inputs, timeout_ms)


async def wait_for_pipewire_spotify_release(
    timeout_ms: int = PIPEWIRE_HANDOFF_RELEASE_TIMEOUT_MS,
) -> bool:
    # A paused Spotify client may retain a corked historical sink-input.  That
    # input is not producing audio and must not block a source handoff.  Only
    # active, audible Spotify inputs are relevant to the quiescence contract.
    def active_spotify_inputs() -> list[dict]:
        return active_unmuted_sink_inputs(list_spotify_sink_inputs())

    return await wait_for_sink_input_release(active_spotify_inputs, timeout_ms)


async def wait_for_spotify_sink_input_samplerate(
    *,
    expected_rate: int | None = None,
    timeout_ms: int = SPOTIFY_SINK_INPUT_RATE_TIMEOUT_MS,
) -> int:
    """Read a stable Spotify stream rate before an entry transition commits."""
    if not isinstance(expected_rate, int) or expected_rate <= 0:
        raise RuntimeError(f"Spotify entry has no valid expected samplerate: {expected_rate}")
    poll_interval_ms = max(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS, 1)
    max_polls = max(1, math.ceil(max(timeout_ms, 0) / poll_interval_ms) + 1)
    last_observation: tuple[object, int] | None = None
    stable_polls = 0
    last_rate: int | None = None
    for poll_index in range(max_polls):
        try:
            entries = await asyncio.to_thread(list_spotify_sink_inputs)
            observation = spotify_sink_input_observation(
                entries,
                expected_rate=expected_rate,
                preferred_identity=(last_observation[0] if last_observation else None),
            )
        except (OSError, subprocess.SubprocessError):
            observation = None
        if observation is not None:
            identity, rate = observation
            last_rate = rate
            if rate == expected_rate and observation == last_observation:
                stable_polls += 1
            elif rate == expected_rate:
                stable_polls = 1
            else:
                # A wrong/transient rate is observed but never accepted as a
                # stable entry result.  The counter also resets on an input
                # identity change so an old Spotify stream cannot validate a
                # newly appeared one.
                stable_polls = 0
            last_observation = (identity, rate)
            if rate == expected_rate and stable_polls >= SPOTIFY_SINK_INPUT_RATE_STABILITY_POLLS:
                return rate
        else:
            # A disappearing input is a new stream boundary.  Do not carry
            # stability across that gap, even if the next input reuses the
            # same PipeWire identity.
            last_observation = None
            stable_polls = 0
        if poll_index + 1 < max_polls:
            await asyncio.sleep(poll_interval_ms / 1000)
    raise RuntimeError(
        "Spotify sink-input samplerate did not become readable and stable "
        f"at the expected rate within {timeout_ms} ms "
        f"(expected={expected_rate} last={last_rate})"
    )


async def wait_for_pipewire_qobuz_release(
    timeout_ms: int = PIPEWIRE_QOBUZ_RELEASE_TIMEOUT_MS,
) -> bool:
    """Quiesce an active qbzd sink input before a guarded graph transition."""
    def active_qobuz_inputs() -> list[dict]:
        return active_unmuted_sink_inputs(list_qobuz_sink_inputs())

    return await wait_for_sink_input_release(active_qobuz_inputs, timeout_ms)


async def wait_for_qobuz_sink_input_samplerate(
    *,
    expected_rate: int | None = None,
    timeout_ms: int = SPOTIFY_SINK_INPUT_RATE_TIMEOUT_MS,
) -> int:
    """Read a stable qbzd stream rate before an entry transition commits."""
    if not isinstance(expected_rate, int) or expected_rate <= 0:
        raise RuntimeError(f"Qobuz entry has no valid expected samplerate: {expected_rate}")
    poll_interval_ms = max(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS, 1)
    max_polls = max(1, math.ceil(max(timeout_ms, 0) / poll_interval_ms) + 1)
    last_observation: tuple[object, int] | None = None
    stable_polls = 0
    last_rate: int | None = None
    for poll_index in range(max_polls):
        try:
            entries = await asyncio.to_thread(list_qobuz_sink_inputs)
            observation = qobuz_sink_input_observation(
                entries,
                expected_rate=expected_rate,
                preferred_identity=(last_observation[0] if last_observation else None),
            )
        except (OSError, subprocess.SubprocessError):
            observation = None
        if observation is not None:
            identity, rate = observation
            last_rate = rate
            if rate == expected_rate and observation == last_observation:
                stable_polls += 1
            elif rate == expected_rate:
                stable_polls = 1
            else:
                stable_polls = 0
            last_observation = (identity, rate)
            if rate == expected_rate and stable_polls >= SPOTIFY_SINK_INPUT_RATE_STABILITY_POLLS:
                return rate
        else:
            last_observation = None
            stable_polls = 0
        if poll_index + 1 < max_polls:
            await asyncio.sleep(poll_interval_ms / 1000)
    raise RuntimeError(
        "qbzd sink-input samplerate did not become readable and stable "
        f"at the expected rate within {timeout_ms} ms "
        f"(expected={expected_rate} last={last_rate})"
    )


async def mpv_source_ports_present() -> bool:
    """Read-only check: are the MPV stream ports and DSP sink ports exposed?

    The mpv PipeWire stream (and with it mpv:output_FL/FR) is published only
    once the stream actually opened after a staged ``loadfile``.  This check
    is the source-startup readiness predicate: while it is false, no link
    mutation may run.
    """
    try:
        links_text = await pw_link.run_pw_link_command("-io")
    except Exception:
        return False
    return all(
        port in links_text
        for port in (
            "mpv:output_FL",
            "mpv:output_FR",
            "fxroute_dsp_sink:playback_FL",
            "fxroute_dsp_sink:playback_FR",
        )
    )


async def wait_for_player_current_file(
    expected_url: str | None,
    timeout_ms: int = 1600,
    *,
    get_player: Callable[[], Any | None],
) -> bool:
    if not expected_url or not get_player():
        return False
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while time.monotonic() <= deadline:
        state = get_player().state
        # ``loadfile`` sets ``current_file`` optimistically before mpv has
        # actually opened the file, so the file path alone is not enough: a
        # follow-up seek (or other mutation) would race the load and mpv
        # rejects it with "error running command".  The source is ready once
        # mpv reports a positive ``duration`` (known-length files/streams) or
        # fires the ``file-loaded`` event (live/unknown-length streams that
        # never report a duration).
        if state.get("current_file") == expected_url and (
            float(state.get("duration") or 0.0) > 0.0
            or bool(state.get("file_loaded"))
        ):
            return True
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
    return False


def get_player_audio_samplerate(player: Any | None) -> Optional[int]:
    if not player or not player._running:
        return None
    try:
        audio_params = player.get_property("audio-params")
    except Exception as exc:
        logger.debug("Failed to read mpv audio-params: %s", exc)
        return None
    if not isinstance(audio_params, dict):
        return None
    rate = audio_params.get("samplerate")
    return rate if isinstance(rate, int) and rate > 0 else None


async def wait_for_player_audio_samplerate(
    timeout_ms: int = PEAK_MONITOR_RATE_MATCH_TIMEOUT_MS,
    *,
    expected_url: str | None = None,
    get_player: Callable[[], Any | None],
    drain_worker: Callable[..., Any],
) -> Optional[int]:
    rate = await drain_worker(get_player_audio_samplerate, get_player())
    state = get_player().state if get_player() else {}
    if rate and (not expected_url or state.get("current_file") == expected_url):
        return rate
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while time.monotonic() <= deadline:
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
        state = get_player().state if get_player() else {}
        if expected_url and state.get("current_file") != expected_url:
            continue
        rate = await drain_worker(get_player_audio_samplerate, get_player())
        if rate:
            return rate
    return None


async def wait_for_radio_live_rate_after_load(
    previous_rate: Optional[int],
    transition_generation: int,
    *,
    timeout_ms: int = RADIO_POST_LOAD_RATE_TIMEOUT_MS,
    get_epoch: Callable[[], int],
    drain_worker: Callable[..., Any],
    get_player: Callable[[], Any | None],
) -> Optional[int]:
    """Wait for the newly loaded station's decoded rate while mpv is paused.

    Accepts a rate that differs from the pre-loadfile rate immediately (the
    new stream's rate), or a rate equal to the pre-loadfile rate once it
    stayed stable across RADIO_POST_LOAD_RATE_STABILITY_POLLS consecutive
    polls (same-rate station switch). Aborts on a stale transition
    generation and on timeout without a valid rate (caller falls back
    safely; no stale pre-loadfile params are used as evidence).
    """
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    stable_same = 0
    while time.monotonic() <= deadline:
        if transition_generation != get_epoch():
            logger.info(
                "Radio post-load rate wait aborted: stale transition "
                "generation=%s current=%s",
                transition_generation,
                get_epoch(),
            )
            return None
        rate = await drain_worker(get_player_audio_samplerate, get_player())
        if isinstance(rate, int) and rate > 0:
            if previous_rate is None or rate != previous_rate:
                return rate
            stable_same += 1
            if stable_same >= RADIO_POST_LOAD_RATE_STABILITY_POLLS:
                return rate
        else:
            stable_same = 0
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
    logger.warning(
        "Radio post-load rate wait timed out after %sms: previous_rate=%s",
        timeout_ms,
        previous_rate,
    )
    return None
