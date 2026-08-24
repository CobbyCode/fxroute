# SPDX-License-Identifier: AGPL-3.0-only

"""Source handoff operations (quiet, rate, effects, prepare, start, volume).

Extracted from :class:`FxrouteTransitionRuntime`; runs on the composing
adapter instance and reads the attributes declared on the class below.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Mapping
from urllib.parse import unquote

import audio.samplerate as samplerate
import audio.samplerate_orchestration as samplerate_orchestration
import playback.source_policy as source_policy
from streaming.spotify.provider import (
    play as spotify_play,
    next_track as spotify_next,
    previous as spotify_previous,
)
from playback.transition import TransitionRequest

from .deps import PlaybackRuntimeDependencies
from .helpers import RADIO_EXPECTED_SAMPLE_RATE_HZ, SOURCE_HANDOFF_SETTLE_MS

# qbzd reinitializes its PipeWire stream after a pause-suspend before it
# reports Playing (~1.15s measured on .104); the start boundary waits bounded
# for the real Playing edge instead of trusting one immediate status read.
QOBUZ_PLAYING_CONFIRM_TIMEOUT_S = 3.0

logger = logging.getLogger(__name__)


class _RuntimeSourceMixin:
    """Attributes provided by the composing adapter instance."""
    _deps: PlaybackRuntimeDependencies
    _staged_target_url: str | None

    async def quiet_old_source(self, request: TransitionRequest) -> None:
        if request.graph_only:
            # A graph-only reconciliation must not pause, reload, or otherwise
            # disturb the source.  The coordinator still owns the output gate.
            return
        if request.source == "spotify" and request.operation == "recovery" and request.reload_source and request.should_play:
            # A Spotify samplerate recovery must release the old sink input
            # before the Coordinator changes the hardware rate and starts
            # Spotify again.  This is intentionally kept inside the
            # Coordinator-owned quiet stage.
            await self._deps.spotify_pause()
            released = await self._deps.wait_for_pipewire_spotify_release()
            if not released:
                await asyncio.sleep(SOURCE_HANDOFF_SETTLE_MS / 1000)
            return

        if (
            request.operation in {"measurement-entry", "output-mode-switch", "sample-rate-policy"}
            and source_policy.is_external_source(request.source)
        ):
            # Guarded graph transitions pause an external owner's own renderer
            # instead of performing a cross-source handoff. Native MPV sources
            # keep the normal quiet/release path below.
            if request.source == "spotify":
                spotify_state = await self._deps.get_spotify_ui_state()
                if self._deps.is_spotify_playback_active(spotify_state):
                    await self._deps.spotify_pause()
                    if not await self._deps.wait_for_pipewire_spotify_release():
                        raise RuntimeError(
                            "active Spotify sink input did not quiesce before guarded graph transition"
                        )
            elif request.source == "qobuz":
                qobuz_state = await self._deps.get_qobuz_ui_state()
                if self._deps.is_qobuz_playback_active(qobuz_state):
                    await self._deps.qobuz_pause()
                    if not await self._deps.wait_for_pipewire_qobuz_release():
                        raise RuntimeError(
                            "active qbzd sink input did not quiesce before guarded graph transition"
                        )
            return

        if source_policy.is_external_source(request.source):
            # An external renderer claims playback: pause any active MPV
            # source and any other active external renderer (Spotify or
            # qbzd), so exactly one source produces audio after the claim.
            local_state = dict(self._player.state if self._player else {})
            local_track = self._deps.get_current_track_info() or {}
            if (
                source_policy.is_mpv_source(local_track.get("source"))
                and self._deps.has_local_footer_context(local_state)
            ):
                await self._deps.pause_local_playback_for_spotify_broadcast()
            if request.source == "qobuz":
                spotify_state = await self._deps.get_spotify_ui_state()
                if self._deps.is_spotify_playback_active(spotify_state):
                    await self._deps.pause_spotify_for_local_playback_broadcast()
                    if not await self._deps.wait_for_pipewire_spotify_release():
                        raise RuntimeError(
                            "active Spotify sink input did not quiesce before Qobuz handoff"
                        )
            elif request.source == "spotify":
                qobuz_state = await self._deps.get_qobuz_ui_state()
                if self._deps.is_qobuz_playback_active(qobuz_state):
                    await self._deps.qobuz_pause()
                    if not await self._deps.wait_for_pipewire_qobuz_release():
                        raise RuntimeError(
                            "active qbzd sink input did not quiesce before Spotify handoff"
                        )
            return

        # A native MPV source claims playback: pause any active external
        # renderer (Spotify or qbzd).
        # Both provider states are read concurrently; the checks and pauses
        # below keep their original order.
        spotify_state, qobuz_state = await asyncio.gather(
            self._deps.get_spotify_ui_state(),
            self._deps.get_qobuz_ui_state(),
        )
        if self._deps.is_spotify_playback_active(spotify_state):
            await self._deps.pause_spotify_for_local_playback_broadcast()
            # The output gate is already closed at this Coordinator stage.
            # Do not touch rate/DSP/helper state until the active Spotify
            # stream has disappeared. Corked historical inputs are ignored by
            # the read-only release helper and therefore need not vanish.
            if not await self._deps.wait_for_pipewire_spotify_release():
                raise RuntimeError(
                    "active Spotify sink input did not quiesce before MPV handoff"
                )
        if self._deps.is_qobuz_playback_active(qobuz_state):
            await self._deps.qobuz_pause()
            if not await self._deps.wait_for_pipewire_qobuz_release():
                raise RuntimeError(
                    "active qbzd sink input did not quiesce before MPV handoff"
                )
        if not self._deps.player_is_running():
            return
        state = self._player.state
        set_volume = getattr(self._player, "set_volume", None)
        if callable(set_volume):
            set_volume(0)
        if (
            request.operation == "recovery"
            and state.get("current_file")
            and not request.rate_change
        ):
            self._player.set_pause(True)
            return
        # A healthy same-rate replacement keeps the existing MPV/PipeWire
        # stream alive and only quiets it.  A real rate change must release the
        # old stream before the target-rate negotiation begins.
        self._player.set_pause(True)
        should_release = bool(
            request.rate_change
            and request.operation not in {"measurement-entry", "output-mode-switch"}
            and (
                request.operation != "sample-rate-policy"
                or request.reload_source
            )
            and state.get("current_file")
        )
        if should_release:
            self._player.stop_playback()
            released = await self._deps.wait_for_pipewire_mpv_release()
            if not released:
                await asyncio.sleep(SOURCE_HANDOFF_SETTLE_MS / 1000)

    async def resolve_target_rate(self, request: TransitionRequest) -> int | None:
        """Resolve a post-load source rate while the output gate is closed."""
        if (
            request.source == "local"
            and request.reload_source
            and request.target_rate is None
        ):
            if not request.target_url:
                raise RuntimeError("Local playback fallback has no target URL")
            if not self._deps.player_is_running():
                raise RuntimeError("MPV player is not available")
            set_volume = getattr(self._player, "set_volume", None)
            if callable(set_volume):
                set_volume(0)
            self._deps.load_player_paused(request.target_url)
            if not await self._deps.wait_for_player_current_file(request.target_url):
                raise RuntimeError("local target did not settle while paused")
            live_rate = await self._deps.wait_for_player_audio_samplerate(
                expected_url=request.target_url,
            )
            if not isinstance(live_rate, int) or live_rate <= 0:
                raise RuntimeError(
                    "local target MPV audio-params did not expose a valid samplerate"
                )
            self._staged_target_url = request.target_url
            logger.info(
                "Local target samplerate resolved from MPV audio-params while paused: "
                "url=%s rate=%s",
                request.target_url,
                live_rate,
            )
            return samplerate.effective_playback_rate(live_rate, request.sample_rate_policy or None)

        if request.source != "radio" or not request.reload_source:
            return request.target_rate
        if not request.target_url:
            return request.target_rate
        if not self._deps.player_is_running():
            raise RuntimeError("MPV player is not available")

        previous_rate = self._deps.get_player_audio_samplerate()
        set_volume = getattr(self._player, "set_volume", None)
        if callable(set_volume):
            set_volume(0)
        self._deps.load_player_paused(request.target_url)
        if not await self._deps.wait_for_player_current_file(request.target_url):
            raise RuntimeError("radio target stream did not settle while paused")
        attempt_epoch = request.attempt_epoch
        if not isinstance(attempt_epoch, int):
            attempt_epoch = self._deps.get_transition_epoch()
        live_rate = await self._deps.wait_for_radio_live_rate_after_load(
            previous_rate,
            attempt_epoch,
        )
        if not isinstance(live_rate, int) or live_rate <= 0:
            live_rate = RADIO_EXPECTED_SAMPLE_RATE_HZ
            logger.warning(
                "Radio target rate unavailable while paused; using safe fallback=%s url=%s",
                live_rate,
                request.target_url,
            )
        self._staged_target_url = request.target_url
        return samplerate.effective_playback_rate(live_rate, request.sample_rate_policy or None)

    async def establish_target_rate(self, request: TransitionRequest) -> None:
        if request.graph_only:
            return
        if not isinstance(request.target_rate, int) or request.target_rate <= 0:
            raise RuntimeError("Playback transition has no target sample rate")
        try:
            status = dict(
                await asyncio.to_thread(self._deps.get_samplerate_status)
            )
        except Exception:
            status = {}
        if samplerate.playback_rate_aligned(status, request.target_rate):
            # The no-op is only safe while the target rate is actually
            # anchored: the force pin already holds it, or it equals the graph
            # default (an unpinned sink renegotiates to the default whenever
            # the graph rebuilds, so a non-default unpinned reading is not
            # stable through the rest of the transition).  Otherwise the pin
            # must be applied now, or the helper/sink can diverge from the
            # frozen target after a stopped output switch.
            force_rate = status.get("force_rate")
            default_rate = status.get("default_rate")
            if force_rate == request.target_rate or (
                force_rate in {None, 0}
                and isinstance(default_rate, int)
                and request.target_rate == default_rate
            ):
                logger.info(
                    "Playback transition target-rate no-op: rate=%s operation=%s source=%s force=%s default=%s",
                    request.target_rate,
                    request.operation,
                    request.source,
                    force_rate,
                    default_rate,
                )
                return
        aligned = await self._deps.ensure_playback_samplerate_force(
            request.target_rate,
            f"coordinator:{request.operation}:{request.source}",
            allow_measurement_session=(request.operation == "measurement-restore"),
            policy=samplerate_orchestration.RADIO_POLICY,
        )
        if not aligned:
            aligned = await self._deps.trigger_idle_sink_renegotiation(request.target_rate)
        if not aligned:
            status = await asyncio.to_thread(self._deps.get_samplerate_status)
            raise RuntimeError(
                "target hardware rate did not settle: "
                f"expected={request.target_rate} active={status.get('active_rate')} "
                f"force={status.get('force_rate')}"
            )

    async def establish_effects_and_helper(
        self, request: TransitionRequest
    ) -> dict[str, Any]:
        if request.operation == "pause":
            return {"dsp_reinitialized": False, "helper_rebuilt": False}
        if not isinstance(request.target_rate, int) or request.target_rate <= 0:
            return {"dsp_reinitialized": False, "helper_rebuilt": False}
        return await self._deps.coordinator_establish_effects_and_helper(request)

    async def prepare_target_source(self, request: TransitionRequest) -> None:
        if request.graph_only:
            return
        if source_policy.is_external_source(request.source):
            return
        if not self._deps.player_is_running():
            raise RuntimeError("MPV player is not available")

        if not request.reload_source and not request.should_play:
            self._player.set_pause(True)
            return

        set_volume = getattr(self._player, "set_volume", None)
        if callable(set_volume):
            set_volume(0)

        if request.native_queue:
            set_shuffle = getattr(self._player, "set_shuffle", None)
            if callable(set_shuffle):
                # Disable any legacy MPV-side permutation before staging or
                # jumping within the explicit FXRoute queue order.
                set_shuffle(False)
            queue_tracks = tuple(request.native_queue)
            if request.reload_source:
                start_index = request.native_queue_index
                if start_index is None:
                    start_index = 0
                if start_index < 0 or start_index >= len(queue_tracks):
                    raise RuntimeError(f"native MPV queue start index is out of range: {start_index}")
                first_url = str(queue_tracks[0].get("url") or "")
                if not first_url:
                    raise RuntimeError("native MPV queue has no first URL")
                self._deps.load_player_paused(first_url)
                for queued_track in queue_tracks[1:]:
                    queued_url = str(queued_track.get("url") or "")
                    if not queued_url:
                        raise RuntimeError("native MPV queue contains an empty URL")
                    self._player.loadfile(queued_url, mode="append")
                self._player.set_loop_playlist(bool(request.native_queue_loop))
                self._player.set_playlist_pos(start_index)
                self._staged_target_url = request.target_url
            else:
                self._player.set_pause(True)

            if not await self._deps.wait_for_player_current_file(request.target_url):
                raise RuntimeError("native MPV queue target did not settle while paused")

        if request.reload_source:
            if not request.target_url:
                raise RuntimeError("Playback transition has no target URL")
            if request.native_queue:
                # The native queue branch already staged the target and the
                # complete MPV playlist.  Do not replace it with app_replace.
                pass
            elif self._staged_target_url != request.target_url:
                # A non-native transition must not inherit loop/shuffle
                # controls from a previously committed native playlist.
                self._player.set_loop_playlist(False)
                set_shuffle = getattr(self._player, "set_shuffle", None)
                if callable(set_shuffle):
                    set_shuffle(False)
                self._deps.load_player_paused(request.target_url)
                self._staged_target_url = request.target_url
                if not await self._deps.wait_for_player_current_file(request.target_url):
                    raise RuntimeError("target MPV stream did not settle while paused")
            else:
                self._player.set_pause(True)

        if (
            request.operation in {"measurement-restore", "replay", "sample-rate-policy"}
            and request.source == "local"
            and request.restore_position is not None
        ):
            position = max(0.0, float(request.restore_position))
            seek = getattr(self._player, "seek", None)
            if not callable(seek):
                raise RuntimeError("MPV position restore is not available")
            self._player.set_pause(True)
            seek(position)
            get_property = getattr(self._player, "get_property", None)
            if callable(get_property):
                try:
                    readback = get_property("time-pos")
                except Exception as exc:
                    raise RuntimeError(
                        f"MPV position restore readback failed: {exc}"
                    ) from exc
                if isinstance(readback, (int, float)) and abs(float(readback) - position) > 0.5:
                    raise RuntimeError(
                        "MPV position restore was not confirmed: "
                        f"expected={position} actual={readback}"
                    )
            logger.info(
                "Local playback position restored under output gate: "
                "url=%s position=%.3f",
                request.target_url,
                position,
            )

        if not await self._deps.ensure_mpv_to_dsp_links():
            raise RuntimeError("target source to DSP links were not confirmed")
        if not request.should_play:
            self._player.set_pause(True)

    async def start_target_source(self, request: TransitionRequest) -> None:
        if request.graph_only:
            return
        if request.source == "spotify":
            if request.operation == "spotify-next":
                data = await spotify_next()
            elif request.operation == "spotify-previous":
                data = await spotify_previous()
            elif request.should_play:
                data = await spotify_play()
            else:
                await self._deps.spotify_pause()
                data = {"status": "Paused"}
            if request.should_play and data.get("status") not in {"Playing", "playing"}:
                raise RuntimeError(f"Spotify did not enter Playing state: {data}")
            return
        if request.source == "qobuz":
            if request.should_play:
                # UI starts ride the same start boundary as Spotify: actually
                # start qbzd (a no-op when a Connect claim already plays),
                # then validate the Playing state before the commit.
                await self._deps.qobuz_play()
                # qbzd resumes asynchronously: after a pause-suspend the audio
                # thread reinitializes the PipeWire stream (~1s) before it
                # reports Playing, so a single immediate status read would see
                # the stale Paused state and abort the handoff.  Wait bounded
                # for the real Playing edge instead, mirroring the MPV IPC
                # readback loop below.
                deadline = time.monotonic() + QOBUZ_PLAYING_CONFIRM_TIMEOUT_S
                last_state: dict[str, Any] = {}
                while time.monotonic() <= deadline:
                    qobuz_state = await self._deps.get_qobuz_ui_state()
                    last_state = qobuz_state
                    if qobuz_state.get("status") in {"Playing", "playing"}:
                        break
                    await asyncio.sleep(0.05)
                if last_state.get("status") not in {"Playing", "playing"}:
                    raise RuntimeError(f"Qobuz did not enter Playing state: {last_state}")
            else:
                qobuz_state = await self._deps.get_qobuz_ui_state()
            return
        if not self._deps.player_is_running():
            raise RuntimeError("MPV player is not available")
        self._player.set_pause(not request.should_play)
        if not request.should_play:
            return
        deadline = time.monotonic() + 1.8
        last_readback: dict[str, Any] = {}
        get_property = getattr(self._player, "get_property", None)
        while time.monotonic() <= deadline:
            if callable(get_property):
                try:
                    live_path = get_property("path")
                    live_paused = get_property("pause")
                    live_idle = get_property("idle-active")
                    live_time_pos = get_property("time-pos")
                    live_audio_params = get_property("audio-params")
                    last_readback = {
                        "path": live_path,
                        "pause": live_paused,
                        "idle-active": live_idle,
                        "time-pos": live_time_pos,
                        "audio-params": live_audio_params,
                    }
                    time_active = isinstance(live_time_pos, (int, float))
                    audio_active = isinstance(live_audio_params, Mapping) and bool(live_audio_params)
                    path_matches = not request.target_url or live_path == request.target_url
                    if (
                        not path_matches
                        and isinstance(live_path, str)
                        and live_path.startswith("file://")
                    ):
                        path_matches = unquote(live_path[7:]) == request.target_url
                    if (
                        path_matches
                        and live_paused is False
                        and live_idle is False
                        and time_active
                        and audio_active
                    ):
                        logger.info(
                            "Playback target start confirmed by MPV IPC: path=%s time_pos=%s audio=%s",
                            live_path,
                            live_time_pos,
                            live_audio_params,
                        )
                        return
                except Exception as exc:
                    last_readback = {"error": str(exc)}
            else:
                # Small test adapters may only expose the cached state.  The
                # production MPVWrapper always has get_property(), so the
                # actual runtime contract above remains IPC-based.
                state = self._player.state
                if (
                    (not request.target_url or state.get("current_file") == request.target_url)
                    and not state.get("paused")
                    and state.get("playing")
                    and isinstance(state.get("position"), (int, float))
                ):
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError(
            "target MPV stream did not pass live IPC start readback: "
            f"{last_readback}"
        )

    async def set_source_volume(self, volume: int, transition_id: str) -> None:
        set_volume = getattr(self._player, "set_volume", None) if self._deps.player_is_running() else None
        if callable(set_volume):
            set_volume(volume)
            # set_volume short-circuits when the cached state already matches,
            # which can mask a muted MPV if the async listener state is stale.
            # Re-assert the volume directly at the audible commit boundary so a
            # silent source cannot commit unnoticed.
            set_property = getattr(self._player, "set_property", None)
            if callable(set_property):
                try:
                    set_property("volume", max(0, min(100, int(volume))))
                except Exception as exc:
                    logger.warning("Playback transition source volume re-assert failed: %s", exc)
        logger.info(
            "Playback transition source volume=%s transition_id=%s",
            volume,
            transition_id,
        )

    async def pause_source_after_failure(self, request: TransitionRequest) -> None:
        if request.source == "spotify":
            try:
                await self._deps.spotify_pause()
            except Exception:
                pass
            return
        if request.source == "qobuz":
            try:
                await self._deps.qobuz_pause()
            except Exception:
                pass
            return
        if self._deps.player_is_running():
            try:
                # Keep the safety invariant even when a lightweight test
                # adapter (or a partially initialized player) does not expose
                # a volume setter: pausing the source must never be skipped
                # because the preceding best-effort attenuation failed.
                set_volume = getattr(self._player, "set_volume", None)
                if callable(set_volume):
                    try:
                        set_volume(0)
                    except Exception:
                        logger.warning("Failed to attenuate MPV after transition failure", exc_info=True)
                self._player.set_pause(True)
            except Exception:
                logger.warning("Failed to pause MPV after transition failure", exc_info=True)

