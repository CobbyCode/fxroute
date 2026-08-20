# SPDX-License-Identifier: AGPL-3.0-only
"""Spotify playerctl transport watcher and samplerate-mismatch detection.

Owns the watch loop, the coalesced detect task and the 1s trigger throttle,
together with the mismatch-detection behavior moved out of ``main.py``.
No imports from ``main``: the recovery and state-refresh entry points are
injected by the composition root; the Spotify client helpers come from
``streaming.spotify``.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass
from typing import Any, Callable

import audio.samplerate as samplerate
from streaming.spotify.mpris import (
    _stop_process,
    detect_backend,
    resolve_player_name,
    spotify_installed,
)
from streaming.spotify.provider import SPOTIFY_PREARM_SAMPLE_RATE_HZ

logger = logging.getLogger(__name__)

SPOTIFY_SINK_INPUT_RATE_STABILITY_POLLS = 2


@dataclass
class SpotifyWatchDependencies:
    """Live services the Spotify playerctl watcher needs."""

    get_playback_state: Callable[[], Any]
    get_spotify_ui_state: Callable[..., Any]
    list_spotify_sink_inputs: Callable[[], list[dict]]
    spotify_sink_input_observation: Callable[..., Any]
    request_coordinated_recovery: Callable[..., Any]
    schedule_spotify_state_refresh: Callable[[str], None]
    claim_spotify_playback: Callable[[str], Any]


class SpotifyPlayerctlWatch:
    """Single owner of the playerctl watch loop, detect task and throttle."""

    def __init__(self, deps: SpotifyWatchDependencies) -> None:
        self._deps = deps
        self.last_trigger_at: float = 0.0
        self.watch_task: asyncio.Task | None = None
        self.detect_task: asyncio.Task | None = None

    async def _event_detect_check(self, reason: str) -> None:
        deps = self._deps
        try:
            burst_delays = (0.05, 0.15, 0.30, 0.60)
            last_snapshot: tuple[object, object, object, object] | None = None
            mismatch_signature: tuple[object, int, int] | None = None
            mismatch_readbacks = 0
            for index, delay_s in enumerate(burst_delays):
                if delay_s > 0:
                    await asyncio.sleep(delay_s if index == 0 else max(0.0, delay_s - burst_delays[index - 1]))
                spotify_inputs = deps.list_spotify_sink_inputs()
                spotify_observation = deps.spotify_sink_input_observation(spotify_inputs)
                spotify_identity = spotify_observation[0] if spotify_observation else None
                spotify_rate = spotify_observation[1] if spotify_observation else None
                spotify_state = await deps.get_spotify_ui_state()
                samplerate_status = samplerate.get_samplerate_status()
                sink_rate = samplerate_status.get("active_rate")
                last_snapshot = (
                    spotify_state.get("status"),
                    len(spotify_inputs),
                    spotify_rate,
                    sink_rate,
                )
                if spotify_state.get("status") == "Playing" and isinstance(spotify_rate, int) and isinstance(sink_rate, int):
                    canonical_rate = SPOTIFY_PREARM_SAMPLE_RATE_HZ
                    target_rate = samplerate.effective_playback_rate(canonical_rate)
                    if spotify_rate != canonical_rate or sink_rate != target_rate:
                        current_mismatch = (spotify_identity, spotify_rate, sink_rate)
                        if current_mismatch == mismatch_signature:
                            mismatch_readbacks += 1
                        else:
                            mismatch_signature = current_mismatch
                            mismatch_readbacks = 1
                        logger.info(
                            "Spotify detect watcher mismatch probe: reason=%s probe=%s/%s stable=%s/%s "
                            "spotify_rate=%s sink_rate=%s title=%s",
                            reason,
                            index + 1,
                            len(burst_delays),
                            mismatch_readbacks,
                            SPOTIFY_SINK_INPUT_RATE_STABILITY_POLLS,
                            spotify_rate,
                            sink_rate,
                            spotify_state.get("title"),
                        )
                        if mismatch_readbacks < SPOTIFY_SINK_INPUT_RATE_STABILITY_POLLS:
                            continue
                        logger.warning(
                            "Stable Spotify samplerate mismatch after transport event; requesting Coordinator recovery: "
                            "reason=%s spotify_rate=%s sink_rate=%s title=%s",
                            reason,
                            spotify_rate,
                            sink_rate,
                            spotify_state.get("title"),
                        )
                        track = {
                            "source": "spotify",
                            "id": spotify_state.get("trackId"),
                            "url": spotify_state.get("trackId"),
                            "title": spotify_state.get("title"),
                            "artist": spotify_state.get("artist"),
                            "sample_rate_hz": SPOTIFY_PREARM_SAMPLE_RATE_HZ,
                        }
                        diagnosis = {
                            "signature": (
                                f"spotify-samplerate:{spotify_identity}:"
                                f"{spotify_rate}->{sink_rate}"
                            ),
                            "source": "spotify",
                            "actual_rate": spotify_rate,
                            "expected_rate": target_rate,
                            "hardware_rate": sink_rate,
                        }
                        await deps.request_coordinated_recovery(
                            track,
                            f"spotify-{reason}",
                            reload_source=True,
                            diagnosis=diagnosis,
                        )
                        break
                    mismatch_signature = None
                    mismatch_readbacks = 0
                    logger.info(
                        "Spotify detect watcher: reason=%s probe=%s/%s status=%s spotify_inputs=%s spotify_rate=%s sink_rate=%s playback_owner=%s title=%s",
                        reason,
                        index + 1,
                        len(burst_delays),
                        spotify_state.get("status"),
                        len(spotify_inputs),
                        spotify_rate,
                        sink_rate,
                        deps.get_playback_state().current_playback_owner,
                        spotify_state.get("title"),
                    )
                    break
                mismatch_signature = None
                mismatch_readbacks = 0
            else:
                if last_snapshot is not None:
                    status, inputs_count, spotify_rate, sink_rate = last_snapshot
                    logger.info(
                        "Spotify detect watcher final: reason=%s probes=%s status=%s spotify_inputs=%s spotify_rate=%s sink_rate=%s playback_owner=%s",
                        reason,
                        len(burst_delays),
                        status,
                        inputs_count,
                        spotify_rate,
                        sink_rate,
                        deps.get_playback_state().current_playback_owner,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Spotify playerctl detect check failed (%s): %s", reason, exc)
        finally:
            if self.detect_task and self.detect_task.done():
                self.detect_task = None

    def schedule_detect(self, reason: str) -> None:
        if self.detect_task and not self.detect_task.done():
            logger.debug(
                "Spotify playerctl detect event coalesced while detect/recovery task is active: reason=%s",
                reason,
            )
            return
        now = time.monotonic()
        if now - self.last_trigger_at < 1.0:
            return
        self.last_trigger_at = now
        self.detect_task = asyncio.create_task(
            self._event_detect_check(reason),
            name="spotify-playerctl-event-detect",
        )

    async def run_watch_loop(self) -> None:
        logger.info("Spotify playerctl watch loop entered")
        if not spotify_installed():
            logger.info("Spotify playerctl watch skipped: Spotify client not installed")
            return
        playerctl_path = shutil.which("playerctl")
        if not playerctl_path:
            logger.info("Spotify playerctl watch skipped: playerctl not available")
            return
        logger.info("Spotify playerctl watch resolved playerctl path: %s", playerctl_path)
        while True:
            proc = None
            try:
                player = await resolve_player_name(await detect_backend())
                logger.info("Spotify playerctl watch spawning follow process: player=%s", player)
                proc = await asyncio.create_subprocess_exec(
                    playerctl_path,
                    f"--player={player}",
                    "metadata",
                    "--follow",
                    "--format",
                    "{{status}}|{{title}}|{{artist}}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                assert proc.stdout is not None
                logger.info("Spotify playerctl watch started")
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    text = line.decode(errors="ignore").strip()
                    if not text:
                        continue
                    status, _, tail = text.partition("|")
                    if status == "Playing":
                        self.schedule_detect(f"playerctl:{tail or 'playing'}")
                        # A real MPRIS Playing event is an external source
                        # claim (Spotify Connect started playback). Fire and
                        # forget: the claim is a no-op when spotify already
                        # owns playback.
                        asyncio.create_task(
                            self._deps.claim_spotify_playback("playerctl-playing"),
                            name="spotify-external-claim",
                        )
                    self._deps.schedule_spotify_state_refresh(f"playerctl:{tail or status or 'metadata'}")
                stderr = b""
                if proc.stderr:
                    try:
                        stderr = await asyncio.wait_for(proc.stderr.read(), timeout=0.2)
                    except Exception:
                        stderr = b""
                if proc.returncode not in (0, None):
                    logger.warning(
                        "Spotify playerctl watch exited with %s: %s",
                        proc.returncode,
                        stderr.decode(errors="ignore").strip() or "no stderr",
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Spotify playerctl watch loop failed: %s", exc)
            finally:
                await _stop_process(proc)
            await asyncio.sleep(1.0)

    async def stop(self) -> None:
        for task in (self.watch_task, self.detect_task):
            if task is not None and not task.done():
                task.cancel()
        self.watch_task = None
        self.detect_task = None
