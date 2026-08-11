# SPDX-License-Identifier: AGPL-3.0-only

"""Measurement window session, sample-rate ownership and the measurement API.

Owns the guarded MeasurementSampleRateSession (48 kHz measurement entry and
coordinator-backed restore), the captured-playback snapshot, the
measurement-entry/pre-arm helpers and the /api/measurements* endpoints.
The store, session instance and cross-domain admission check are supplied by
the composition root. Audio-graph operations still delegate to main's
transition runtime because that remains the owner of playback orchestration.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

import samplerate
import samplerate_orchestration
from measurement import (
    MEASUREMENT_DEFAULT_SAMPLE_RATE,
    measurement_setup_settings_from_payload,
    normalize_measurement_optional_input_channel,
)
from uploads import (
    MEASUREMENT_TEXT_MAX_BYTES,
    UploadTooLargeError,
    read_upload,
)
from playback_transition import TransitionRequest
from samplerate import (
    OUTPUT_MODE_SUBWOOFER_22,
    OUTPUT_MODE_SUBWOOFER_22_STEREO,
    OUTPUT_MODE_SUBWOOFER_22_MODES,
    OUTPUT_MODE_SUBWOOFER_MODES,
)
from subwoofer_runtime import DEFAULT_SAMPLE_RATE, SubwooferRuntimeConfig

logger = logging.getLogger(__name__)

router = APIRouter()


class MeasurementEntryInvalidated(Exception):
    """A measurement start request was invalidated by a session close before commit.

    request_close() increments the session's entry epoch, so any start request
    that captured the previous epoch is rejected at registration time, before
    any 48 kHz / playback / ownership side effects occur.
    """


@dataclass(frozen=True)
class MeasurementServices:
    get_store: Callable[[], Any]
    get_session: Callable[[], Any]
    auto_sub_active: Callable[[], bool]


_services: MeasurementServices | None = None


def configure_services(services: MeasurementServices) -> None:
    global _services
    _services = services


def _measurement_services() -> MeasurementServices:
    if _services is None:
        raise RuntimeError("Measurement services are not configured")
    return _services


_playback_state_before_measurement: dict[str, Any] | None = None


class MeasurementSampleRateSession:
    """Own the PipeWire force-rate for one measurement-window session."""

    def __init__(self) -> None:
        self.active = False
        self.entry_in_progress = False
        self.measurement_rate = 48_000
        self.original_force_rate = 0
        self.active_manual_job_ids: set[str] = set()
        self.active_auto_sub_job_id: str | None = None
        self.active_spl_job_ids: set[str] = set()
        self.close_requested = False
        self.deferred_release_pending = False
        self.generation = 0
        self._entry_epoch = 0
        self.lock = asyncio.Lock()
        self._playback_captured = False
        self._rate_changed = False

    @property
    def owns_audio_graph(self) -> bool:
        """Return whether measurement currently owns the playback graph."""
        return bool(self.entry_in_progress or self.active)

    @property
    def has_active_jobs(self) -> bool:
        """Return whether any measurement job is actually running.

        An open-but-idle measurement window (no sweep/auto-sub/SPL job)
        must not lock playback-rate or output-mode mutations.
        """
        return bool(
            self.active_manual_job_ids
            or self.active_spl_job_ids
            or self.active_auto_sub_job_id is not None
        )

    def capture_entry_epoch(self) -> int:
        """Return the current measurement entry-invalidation epoch.

        Start requests capture this token at their earliest point (before any
        await that could let a close interleave) and pass it to the session
        registration API.  request_close() increments the epoch, so an entry
        captured before a close is rejected at registration time.
        """
        return self._entry_epoch

    def _validate_entry_epoch(self, captured: int | None) -> int:
        """Reject an entry captured in a superseded epoch; return its token."""
        if captured is None:
            return self._entry_epoch
        if captured != self._entry_epoch:
            raise MeasurementEntryInvalidated(
                "Measurement start was cancelled because the measurement window was closed"
            )
        return captured

    async def _start_locked(self, measurement_rate: int) -> int:
        from main import (
            get_samplerate_status,
            _coordinator_current_playback_context,
            _run_coordinated_transition,
        )
        if self.active:
            return self.generation
        # Reset old snapshots so a new session always starts fresh.
        global _playback_state_before_measurement
        _playback_state_before_measurement = None
        self.measurement_rate = int(measurement_rate)
        self._playback_captured = False
        self._rate_changed = False
        self.entry_in_progress = True
        try:
            status = get_samplerate_status()
            self.original_force_rate = int(status.get("force_rate") or 0)
        except Exception as exc:
            logger.warning("Measurement sample-rate session could not read force-rate: %s", exc)
            self.original_force_rate = 0
        try:
            context = await _coordinator_current_playback_context()
            # Capture playback state from the same read-only live context that
            # the guarded measurement-entry transition will pause.  This is
            # especially important for Spotify, where current_track_info and
            # MPV are intentionally unrelated transport state.
            _capture_playback_state_before_measurement(context)
            transition_result = await _run_coordinated_transition(TransitionRequest(
                operation="measurement-entry",
                source=str(context.get("source") or "local"),
                target_rate=self.measurement_rate,
                target_url=context.get("target_url"),
                target_track=dict(context.get("target_track") or {}),
                should_play=False,
                rate_change=self.original_force_rate != self.measurement_rate,
                reload_source=False,
                detail="measurement-entry",
            ))
            if not transition_result.committed:
                raise RuntimeError("measurement entry was not committed")
            self._rate_changed = self.original_force_rate != self.measurement_rate
        except asyncio.CancelledError:
            self.entry_in_progress = False
            raise
        except Exception as exc:
            logger.error(
                "Measurement sample-rate session could not establish its guarded entry at %s Hz: %s",
                self.measurement_rate,
                exc,
            )
            self._playback_captured = False
            _playback_state_before_measurement = None
            self.original_force_rate = 0
            self.entry_in_progress = False
            raise RuntimeError(
                f"Could not establish guarded measurement entry at {self.measurement_rate} Hz"
            ) from exc
        self.active = True
        self.entry_in_progress = False
        self.close_requested = False
        self.deferred_release_pending = False
        logger.info(
            "Measurement sample-rate session started: generation=%s measurement_rate=%s original_force_rate=%s",
            self.generation,
            self.measurement_rate,
            self.original_force_rate,
        )
        return self.generation

    async def start(self, measurement_rate: int, entry_epoch: int | None = None) -> int:
        async with self.lock:
            self._validate_entry_epoch(entry_epoch)
            return await self._start_locked(measurement_rate)

    async def register_manual_job(self, job_id: str, entry_epoch: int | None = None) -> int:
        async with self.lock:
            self._validate_entry_epoch(entry_epoch)
            if not self.active:
                logger.info("Measurement sample-rate session start requested: caller=manual-sweep job_id=%s", job_id)
                await self._start_locked(_resolve_measurement_start_sample_rate())
            self.active_manual_job_ids.add(job_id)
            return self.generation

    async def replace_manual_job(self, old_job_id: str, job_id: str) -> None:
        async with self.lock:
            self.active_manual_job_ids.discard(old_job_id)
            self.active_manual_job_ids.add(job_id)

    async def unregister_manual_job(self, job_id: str) -> None:
        async with self.lock:
            self.active_manual_job_ids.discard(job_id)
            await self._check_release()

    async def register_auto_sub(self, job_id: str, entry_epoch: int | None = None) -> int:
        async with self.lock:
            self._validate_entry_epoch(entry_epoch)
            if not self.active:
                logger.info("Measurement sample-rate session start requested: caller=auto-sub job_id=%s", job_id)
                await self._start_locked(_resolve_measurement_start_sample_rate())
            self.active_auto_sub_job_id = job_id
            return self.generation

    async def register_spl_job(self, job_id: str, entry_epoch: int | None = None) -> int:
        async with self.lock:
            self._validate_entry_epoch(entry_epoch)
            if not self.active:
                logger.info("Measurement sample-rate session start requested: caller=spl-meter job_id=%s", job_id)
                await self._start_locked(_resolve_measurement_start_sample_rate())
            self.active_spl_job_ids.add(job_id)
            return self.generation

    async def unregister_spl_job(self, job_id: str) -> None:
        async with self.lock:
            self.active_spl_job_ids.discard(job_id)
            await self._check_release()

    async def unregister_auto_sub(self, job_id: str) -> None:
        async with self.lock:
            if self.active_auto_sub_job_id == job_id:
                self.active_auto_sub_job_id = None
            await self._check_release()

    async def request_open(self) -> None:
        """Record a heartbeat without changing the audio sample rate."""
        async with self.lock:
            logger.info(
                "Measurement sample-rate session heartbeat: caller=measurement-window-open active=%s action=state-only",
                self.active,
            )
            if self.active:
                self.close_requested = False
                self.deferred_release_pending = False

    async def request_close(self) -> None:
        async with self.lock:
            # Invalidate every start request that captured the epoch before
            # this close: a later registration of such an entry must abort
            # before it can commit or touch the audio graph.
            self._entry_epoch += 1
            self.close_requested = True
            released = await self._check_release()
            if not released:
                self.deferred_release_pending = True

    async def _check_release(self) -> bool:
        if not self.active or not self.close_requested:
            return False
        if self.active_auto_sub_job_id is not None or self.active_manual_job_ids or self.active_spl_job_ids:
            return False
        await self._release()
        return True

    async def _release(self) -> None:
        from main import (
            current_track_info,
            _begin_playback_transition_attempt,
            playback_transition_coordinator,
            _end_playback_transition_attempt,
            _get_current_pipewire_force_rate,
            _set_pipewire_force_rate,
            _ensure_playback_samplerate_force,
            _wait_for_samplerate_alignment,
            _sync_subwoofer_runtime_at_rate,
            get_samplerate_status,
        )
        global _playback_state_before_measurement

        policy = samplerate.load_sample_rate_policy()
        restore_value = int(policy.get("rate") or 0) if policy.get("mode") == "fixed" else 0
        captured_playback_snapshot = _playback_state_before_measurement
        snapshot_is_current = await _measurement_restore_snapshot_matches_current_intent(
            captured_playback_snapshot
        ) if captured_playback_snapshot else False
        if captured_playback_snapshot and not snapshot_is_current:
            logger.info(
                "Measurement playback snapshot discarded: current playback intent "
                "no longer matches the captured track; no old source will be resurrected"
            )
            _playback_state_before_measurement = None
        playback_snapshot = captured_playback_snapshot if snapshot_is_current else {}
        playback_source = playback_snapshot.get("source") or (current_track_info or {}).get("source")
        playback_target_rate = playback_snapshot.get("expected_rate")
        if not isinstance(playback_target_rate, int) or playback_target_rate <= 0:
            playback_target_rate = None
        target_rate = playback_target_rate or restore_value
        force_rate_owned = True
        coordinator_attempted = False
        playback_restore_via_coordinator = bool(
            playback_source in {"local", "radio", "spotify"}
            and playback_target_rate
            and snapshot_is_current
        )
        if playback_restore_via_coordinator:
            coordinator_attempted = True
            attempt_epoch = _begin_playback_transition_attempt()
            track = dict(
                (playback_snapshot.get("track_info") or {})
                if playback_source == "spotify"
                else (current_track_info or {})
            )
            track.update({
                "source": playback_source,
                "url": playback_snapshot.get("url") or playback_snapshot.get("path"),
                "path": playback_snapshot.get("path") or playback_snapshot.get("url"),
                "id": playback_snapshot.get("id"),
                "title": playback_snapshot.get("title"),
            })
            if samplerate.load_sample_rate_policy().get("mode") == "auto":
                track["sample_rate_hz"] = playback_target_rate
            try:
                restore_result = await playback_transition_coordinator.restore_measurement(
                    source=playback_source,
                    target_rate=playback_target_rate,
                    target_url=track.get("url"),
                    target_track=track,
                    should_play=bool(playback_snapshot.get("was_playing")),
                    rate_change=self._rate_changed,
                    reload_source=self._rate_changed,
                    restore_position=(
                        playback_snapshot.get("position")
                        if playback_source == "local"
                        else None
                    ),
                    restore_intent=playback_snapshot,
                    attempt_epoch=attempt_epoch,
                )
                if not restore_result.committed:
                    logger.info(
                        "Measurement restore was skipped because its playback intent changed "
                        "inside the Coordinator contract"
                    )
                    coordinator_attempted = False
                    playback_restore_via_coordinator = False
                    playback_snapshot = {}
                    playback_source = None
                    playback_target_rate = None
                    target_rate = restore_value
                else:
                    self._rate_changed = False
                    _playback_state_before_measurement = None
                    logger.info(
                        "Measurement restore committed through PlaybackTransitionCoordinator: source=%s target_rate=%s",
                        playback_source,
                        playback_target_rate,
                    )
            except Exception as exc:
                logger.warning("Measurement restore through coordinator failed; retaining safe state: %s", exc)
            finally:
                _end_playback_transition_attempt()
        try:
            # Active playback restoration is exclusively coordinator-owned.  A
            # failed or unavailable coordinator must leave the playback gate
            # in its safe state instead of falling through to a second direct
            # force-rate/helper restore path.
            if self._rate_changed and not coordinator_attempted and playback_restore_via_coordinator:
                force_rate_owned = False
                logger.warning(
                    "Measurement sample-rate session playback restore requires the PlaybackTransitionCoordinator; "
                    "direct restore is intentionally suppressed: source=%s target_rate=%s",
                    playback_source,
                    playback_target_rate,
                )
            elif self._rate_changed and not coordinator_attempted:
                current_force_rate = _get_current_pipewire_force_rate()
                if current_force_rate == self.measurement_rate:
                    try:
                        # Set the rate of the playback context directly. Do not
                        # briefly restore the idle/default rate first: that would
                        # create a second transition before the sink is aligned.
                        _set_pipewire_force_rate(target_rate)
                    except Exception as exc:
                        force_rate_owned = False
                        logger.warning("Measurement sample-rate session force-rate restore failed: %s", exc)
                else:
                    force_rate_owned = False
                    logger.warning(
                        "Measurement sample-rate session restore skipped after external force-rate change: "
                        "current_force_rate=%s measurement_rate=%s restore_value=%s target_rate=%s",
                        current_force_rate,
                        self.measurement_rate,
                        restore_value,
                        target_rate,
                    )

            try:
                runtime_restore_rate = playback_target_rate or restore_value
                if runtime_restore_rate <= 0:
                    status = get_samplerate_status()
                    runtime_restore_rate = int(
                        status.get("clock_rate")
                        or DEFAULT_SAMPLE_RATE
                    )

                rate_ready = True
                if coordinator_attempted:
                    rate_ready = False
                elif playback_target_rate and force_rate_owned:
                    if playback_source == "radio":
                        rate_ready = await _ensure_playback_samplerate_force(
                            playback_target_rate,
                            "radio-restart-after-measurement",
                            allow_measurement_session=True,
                            policy=samplerate_orchestration.RADIO_POLICY,
                        )
                    else:
                        rate_ready = await _wait_for_samplerate_alignment(
                            playback_target_rate, timeout_ms=3500,
                        )

                measurement_only_restore = not playback_target_rate or playback_source not in {"local", "radio", "spotify"}
                if rate_ready and not coordinator_attempted and measurement_only_restore:
                    await _sync_subwoofer_runtime_at_rate(runtime_restore_rate, _rate_lock_held=True)
                else:
                    logger.warning(
                        "Measurement sample-rate session runtime restore deferred until playback sink aligns: "
                        "source=%s target_rate=%s",
                        playback_source, runtime_restore_rate,
                    )
            except Exception as exc:
                logger.warning("Measurement sample-rate session runtime restore failed: %s", exc)
        finally:
            completed_generation = self.generation
            self.active = False
            self.active_manual_job_ids.clear()
            self.active_auto_sub_job_id = None
            self.active_spl_job_ids.clear()
            self.close_requested = False
            self.deferred_release_pending = False
            self._playback_captured = False
            self._rate_changed = False
            self.original_force_rate = 0
            _playback_state_before_measurement = None
            self.generation += 1
            logger.info(
                "Measurement sample-rate session released: generation=%s next_generation=%s restore_rate=%s",
                completed_generation,
                self.generation,
                restore_value,
            )

    async def run_watchdog(self) -> None:
        while True:
            try:
                # Heartbeat expiry is not an audio-session close signal. Browser
                # timer suspension can pause heartbeats while the window remains
                # open; only the explicit close request may restore the rate.
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Measurement sample-rate session watchdog failed: %s", exc)
                await asyncio.sleep(5.0)


def _capture_playback_state_before_measurement(
    playback_context: Mapping[str, Any] | None = None,
):
    """Save playback state before measurement starts.

    After measurement at 48 kHz, the active playback stream (paused/playing) is
    stale at the wrong sample rate. This capture enables a controlled restart
    on resume instead of a simple unpause.

    Stores source, url/path, id, title, expected_rate, position, and paused
    flag so the controlled restart can restore the user's exact spot.
    """
    from main import (
        playback_intent_generation,
        _get_player_audio_samplerate,
        current_track_info,
        player_instance,
        SPOTIFY_PREARM_SAMPLE_RATE_HZ,
    )
    measurement_sr_session = _measurement_services().get_session()
    global _playback_state_before_measurement
    if measurement_sr_session is not None and measurement_sr_session._playback_captured:
        return
    _playback_state_before_measurement = None
    context = playback_context if isinstance(playback_context, Mapping) else {}
    if context.get("source") == "spotify":
        spotify_state = dict(context.get("spotify") or {})
        target_track = dict(context.get("target_track") or {})
        spotify_identity = str(
            context.get("target_url")
            or target_track.get("id")
            or target_track.get("url")
            or spotify_state.get("trackId")
            or spotify_state.get("url")
            or ""
        ).strip()
        if not spotify_identity:
            return
        track_id = target_track.get("id") or spotify_state.get("trackId")
        was_playing = bool(context.get("should_play"))
        track_info = dict(target_track)
        track_info.update({
            "source": "spotify",
            "id": track_id,
            "url": spotify_identity,
            "title": track_info.get("title") or spotify_state.get("title"),
            "artist": track_info.get("artist") or spotify_state.get("artist"),
            "sample_rate_hz": SPOTIFY_PREARM_SAMPLE_RATE_HZ,
        })
        effective_rate = samplerate.effective_playback_rate(SPOTIFY_PREARM_SAMPLE_RATE_HZ)
        _playback_state_before_measurement = {
            "source": "spotify",
            "track_info": track_info,
            "url": spotify_identity,
            "path": spotify_identity,
            "current_file": None,
            "id": track_id,
            "spotify_identity": spotify_identity,
            "title": track_info.get("title"),
            "expected_rate": effective_rate,
            "position": 0.0,
            "was_paused": not was_playing,
            "was_playing": was_playing,
            "intent_generation": playback_intent_generation,
        }
        if measurement_sr_session is not None:
            measurement_sr_session._playback_captured = True
        logger.info(
            "PLAYBACK-CAPTURE-DIAG Spotify state captured before measurement: id=%s expected_rate=%s",
            track_id or spotify_identity,
            effective_rate,
        )
        return
    if not current_track_info:
        return
    source = current_track_info.get("source")
    if source not in {"radio", "local"}:
        return
    if not player_instance or not player_instance._running:
        return
    state = player_instance.state
    current_file = state.get("current_file") or ""
    if not current_file or state.get("ended"):
        return

    # Resolve expected rate
    expected_rate = None
    if source == "radio":
        # Read the radio stream's actual decoded sample rate from mpv
        try:
            expected_rate = _get_player_audio_samplerate()
        except Exception as exc:
            logger.warning("PLAYBACK-CAPTURE-DIAG could not read player audio samplerate: %s", exc)
        if not isinstance(expected_rate, int) or expected_rate <= 0:
            expected_rate = None
    elif source == "local":
        # For local tracks, use the track metadata sample rate
        expected_rate = current_track_info.get("sample_rate_hz")
    if not isinstance(expected_rate, int) or expected_rate <= 0:
        logger.warning(
            "PLAYBACK-CAPTURE-DIAG no expected_rate available, skipping capture: source=%s url=%s",
            source, current_track_info.get("url", ""),
        )
        return
    expected_rate = samplerate.effective_playback_rate(expected_rate)

    saved_state = {
        "source": source,
        "track_info": dict(current_track_info),
        "url": current_track_info.get("url", ""),
        "path": current_track_info.get("path", ""),
        "current_file": current_file,
        "id": current_track_info.get("id", ""),
        "title": current_track_info.get("title", ""),
        "expected_rate": expected_rate,
        "position": float(state.get("position", 0) or 0),
        "was_paused": bool(state.get("paused")),
        "was_playing": not state.get("paused") and not state.get("ended"),
        "intent_generation": playback_intent_generation,
    }
    _playback_state_before_measurement = saved_state
    logger.info(
        "PLAYBACK-CAPTURE-DIAG state captured before measurement: source=%s url=%s id=%s "
        "expected_rate=%s position=%.2f was_paused=%s was_playing=%s",
        source, saved_state["url"], saved_state["id"],
        expected_rate, saved_state["position"], saved_state["was_paused"], saved_state["was_playing"],
    )
    if measurement_sr_session is not None:
        measurement_sr_session._playback_captured = True


async def _measurement_entry_preflight(measurement_rate: int = MEASUREMENT_DEFAULT_SAMPLE_RATE) -> None:
    """Validate the guarded measurement state before creating a sweep job."""
    from main import (
        playback_transition_coordinator,
        get_samplerate_status,
        _reconcile_transition_sink_rate,
        _playback_graph_diagnosis,
        _log_playback_graph_diagnosis,
    )
    services = _measurement_services()
    measurement_sr_session = services.get_session()
    measurement_store = services.get_store()
    coordinator = playback_transition_coordinator
    if coordinator is None:
        raise RuntimeError("PlaybackTransitionCoordinator is not available for measurement entry")
    if coordinator.transition_blocked:
        raise RuntimeError("measurement entry is blocked by an active or latched playback transition")

    status = dict(get_samplerate_status())
    if not samplerate.playback_rate_aligned(status, measurement_rate):
        await _reconcile_transition_sink_rate(
            measurement_rate, reason="measurement-entry-preflight"
        )
        status = dict(get_samplerate_status())
    if status.get("active_rate") != measurement_rate:
        raise RuntimeError(
            "measurement entry preflight rate mismatch: "
            f"expected={measurement_rate} actual={status.get('active_rate')}"
        )
    if status.get("force_rate") not in {None, 0, measurement_rate}:
        raise RuntimeError(
            "measurement entry preflight force-rate mismatch: "
            f"expected={measurement_rate} actual={status.get('force_rate')}"
        )

    diagnosis = await _playback_graph_diagnosis(
        target_rate=measurement_rate,
        require_source=False,
    )
    if not diagnosis.get("links_complete"):
        session_active = bool(
            measurement_sr_session is not None
            and getattr(measurement_sr_session, "active", False)
        )
        reconciler = getattr(
            coordinator,
            "reconcile_measurement_session",
            None,
        )
        if session_active and callable(reconciler):
            reconciled_state = await reconciler(
                target_rate=measurement_rate,
                initial_graph=diagnosis,
            )
            if not isinstance(reconciled_state, Mapping) or not (
                reconciled_state.get("graph_complete") is True
                or reconciled_state.get("links_complete") is True
            ):
                raise RuntimeError(
                    "measurement session reconcile did not confirm a complete canonical graph"
                )
        else:
            _log_playback_graph_diagnosis(
                diagnosis,
                target_rate=measurement_rate,
                reason="measurement-entry-preflight",
                detail="before-sweep",
            )
            raise RuntimeError("measurement entry preflight found an incomplete canonical graph")

    if measurement_store is not None:
        playback_target = measurement_store._resolve_playback_target()
        if not isinstance(playback_target, Mapping) or not playback_target.get("target_name"):
            raise RuntimeError("measurement entry preflight found no playback target")
        playback_route = measurement_store._build_measurement_playback_route(
            "fxroute-measure-preflight",
            playback_target,
        )
        if not isinstance(playback_route, Mapping) or not playback_route.get("route"):
            raise RuntimeError("measurement entry preflight found no playback route")
        target_name = str(playback_route.get("playback_target_name") or "").strip()
        list_ports = getattr(measurement_store, "_list_pw_ports", None)
        if not target_name or not callable(list_ports):
            raise RuntimeError("measurement entry preflight could not inspect playback route ports")
        try:
            ports = set(str(port) for port in list_ports(target_name))
        except Exception as exc:
            raise RuntimeError(f"measurement entry preflight could not read playback route ports: {exc}") from exc
        required_ports = {
            f"{target_name}:playback_FL",
            f"{target_name}:playback_FR",
        }
        if not required_ports.issubset(ports):
            raise RuntimeError(
                "measurement entry preflight playback route is incomplete: "
                f"target={target_name} missing={sorted(required_ports - ports)}"
            )


async def _measurement_restore_snapshot_matches_current_intent(
    snapshot: Mapping[str, Any] | None,
) -> bool:
    """Return whether a captured playback snapshot is still user-intended."""
    from main import (
        _spotify_intent_matches_live_state,
        _spotify_snapshot_identity_values,
        _local_intent_matches_live_state,
    )
    if not snapshot:
        return False
    expected_source = str(snapshot.get("source") or "")
    if expected_source == "spotify":
        return await _spotify_intent_matches_live_state(
            _spotify_snapshot_identity_values(snapshot),
            snapshot.get("intent_generation"),
        )

    expected_track = snapshot.get("track_info") or {}
    expected_id = snapshot.get("id") or expected_track.get("id")
    expected_url = (
        snapshot.get("url")
        or snapshot.get("path")
        or expected_track.get("url")
        or expected_track.get("path")
    )
    return _local_intent_matches_live_state(
        expected_source=expected_source,
        expected_id=expected_id,
        expected_url=expected_url,
        expected_file=snapshot.get("current_file") or expected_url,
        intent_generation=snapshot.get("intent_generation"),
    )


def _resolve_measurement_start_sample_rate() -> int:
    measurement_store = _measurement_services().get_store()
    if measurement_store is not None and hasattr(measurement_store, "_resolve_measurement_sample_rate"):
        try:
            sample_rate = int(measurement_store._resolve_measurement_sample_rate())
            if sample_rate > 0:
                return sample_rate
        except Exception as exc:
            logger.warning("Measurement sample-rate resolution failed, using 48000 Hz fallback: %s", exc)
    return 48_000


async def _wait_for_selected_output_effective_rate(expected_rate: int, timeout_ms: int = 3000) -> tuple[bool, dict]:
    from main import (
        get_audio_output_overview,
        PIPEWIRE_HANDOFF_POLL_INTERVAL_MS,
    )
    last_overview: dict = {}
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while time.monotonic() <= deadline:
        last_overview = get_audio_output_overview()
        output_mode = last_overview.get("output_mode") or {}
        effective_rate = output_mode.get("effective_output_rate")
        if isinstance(effective_rate, int) and effective_rate == expected_rate:
            return True, last_overview
        await asyncio.sleep(PIPEWIRE_HANDOFF_POLL_INTERVAL_MS / 1000)
    if not last_overview:
        last_overview = get_audio_output_overview()
    return False, last_overview


def _measurement_helper_snapshot_summary(snapshot: dict | None) -> dict:
    """Thin wrapper: helper snapshot summary lives in samplerate (REFACTOR-003)."""
    return samplerate.measurement_helper_snapshot_summary(snapshot)


def _log_22_measurement_sweep_config(config: SubwooferRuntimeConfig, snapshot: dict | None) -> None:
    if config.output_mode not in OUTPUT_MODE_SUBWOOFER_22_MODES:
        return
    snapshot = snapshot or {}
    logger.info(
        "2.2 measurement sweep config: mode=%s sub1_alignment_ms=%.2f sub2_alignment_ms=%.2f "
        "derived_main=%.2f derived_sub1=%.2f derived_sub2=%.2f helper_pid=%s helper_args=%s",
        config.output_mode,
        config.sub_alignment_ms,
        config.sub2_alignment_ms,
        config.derived_main_delay_ms,
        config.derived_sub1_delay_ms,
        config.derived_sub2_delay_ms,
        snapshot.get("helper_pid"),
        snapshot.get("helper_args"),
    )


def _build_measurement_audio_output_context() -> dict:
    """Build audio_output_context metadata for measurement saves."""
    from main import (
        get_audio_output_overview,
        subwoofer_runtime,
    )
    context: dict = {}
    try:
        overview = get_audio_output_overview()
        output_mode = overview.get("output_mode") or {}
        mode = str(output_mode.get("mode", "stereo") or "stereo")
        if mode in OUTPUT_MODE_SUBWOOFER_MODES:
            config = SubwooferRuntimeConfig.from_overview(overview)
            snapshot = subwoofer_runtime.snapshot() if subwoofer_runtime is not None else {}
            context["output_mode"] = mode
            context["output_key"] = config.output_key
            context["output_label"] = config.output_label
            context["output_channels"] = config.output_channels
            context["sample_rate_hz"] = config.sample_rate
            context["crossover_frequency_hz"] = config.crossover_frequency_hz
            context["crossover_type"] = "LR24"
            context["main_highpass_enabled"] = config.main_highpass_enabled
            context["sub_level_db"] = config.sub_level_db
            context["sub_alignment_ms"] = config.sub_alignment_ms
            context["derived_main_delay_ms"] = config.derived_main_delay_ms
            context["derived_sub_delay_ms"] = config.derived_sub_delay_ms
            context["derived_sub1_delay_ms"] = config.derived_sub1_delay_ms
            context["derived_sub2_delay_ms"] = config.derived_sub2_delay_ms
            context["sub_polarity"] = config.sub_polarity
            context["sub2_level_db"] = config.sub2_level_db
            context["sub2_alignment_ms"] = config.sub2_alignment_ms
            context["sub2_polarity"] = config.sub2_polarity
            context["runtime_active"] = snapshot.get("active")
            context["helper_pid"] = snapshot.get("helper_pid")
        else:
            context["output_mode"] = "stereo"
    except Exception:
        logger.warning("Failed to build audio output measurement context", exc_info=True)
        context["output_mode"] = "unknown"
    return context


async def _sync_subwoofer_runtime_for_measurement_sweep(measurement_rate: int) -> None:
    from main import (
        subwoofer_runtime,
        get_audio_output_overview,
        get_samplerate_status,
        _pulse_suspend_sink_for_samplerate,
        _audio_output_overview_with_effective_rate,
        _sync_subwoofer_runtime,
    )
    if subwoofer_runtime is None:
        return None
    overview = get_audio_output_overview()
    output_mode = overview.get("output_mode") or {}
    if output_mode.get("mode") not in OUTPUT_MODE_SUBWOOFER_MODES:
        return None
    mode_num = "2.2 Stereo Bass" if output_mode.get("mode") == OUTPUT_MODE_SUBWOOFER_22_STEREO else "2.2" if output_mode.get("mode") == OUTPUT_MODE_SUBWOOFER_22 else "2.1"
    output_key = str(output_mode.get("effective_output_key") or "").strip()

    samplerate_status = get_samplerate_status()
    previous_force_rate = samplerate_status.get("force_rate")
    previous_active_rate = samplerate_status.get("active_rate")
    before = subwoofer_runtime.snapshot()
    logger.info(
        "%s measurement pre-arm starting: measurement_rate=%s samplerate_before=%s helper_before=%s",
        mode_num,
        measurement_rate,
        json.dumps(
            {
                "active_rate": previous_active_rate,
                "force_rate": previous_force_rate,
            },
            sort_keys=True,
        ),
        json.dumps(_measurement_helper_snapshot_summary(before), sort_keys=True),
    )
    logger.info(
        "%s measurement samplerate session pre-arm: target_rate=%s active_rate=%s force_rate=%s",
        mode_num,
        measurement_rate,
        previous_active_rate,
        previous_force_rate,
    )
    if previous_active_rate != measurement_rate:
        _pulse_suspend_sink_for_samplerate(output_key, "measurement-pre-arm")

    overview = _audio_output_overview_with_effective_rate(get_audio_output_overview(), measurement_rate)
    await _sync_subwoofer_runtime(overview, reason="measurement-pre-arm")

    aligned, overview = await _wait_for_selected_output_effective_rate(measurement_rate, timeout_ms=3500)
    if not aligned:
        output_mode = overview.get("output_mode") or {}
        effective_rate = output_mode.get("effective_output_rate")
        raise RuntimeError(
            f"{mode_num} measurement pre-arm failed: selected output did not reach "
            f"{measurement_rate} Hz before sweep start (effective_rate={effective_rate})"
        )

    await _sync_subwoofer_runtime(overview, reason="measurement-pre-arm")
    after = subwoofer_runtime.snapshot()
    samplerate_after = get_samplerate_status()
    after_config = after.get("config") or {}
    runtime_config = SubwooferRuntimeConfig.from_overview(overview)
    helper_rate = after_config.get("sample_rate")
    if not after.get("active") or helper_rate != measurement_rate:
        raise RuntimeError(
            f"{mode_num} measurement pre-arm failed: helper did not settle at "
            f"{measurement_rate} Hz before sweep start (active={after.get('active')} sample_rate={helper_rate})"
        )

    logger.info(
        "%s measurement helper pre-armed before sweep: target_rate=%s helper_rate_before=%s helper_rate_after=%s "
        "helper_pid=%s samplerate_after=%s helper_after=%s",
        mode_num,
        measurement_rate,
        (before.get("config") or {}).get("sample_rate"),
        helper_rate,
        after.get("helper_pid"),
        json.dumps(
            {
                "active_rate": samplerate_after.get("active_rate"),
                "force_rate": samplerate_after.get("force_rate"),
            },
            sort_keys=True,
        ),
        json.dumps(_measurement_helper_snapshot_summary(after), sort_keys=True),
    )
    _log_22_measurement_sweep_config(runtime_config, after)
    return None


async def _unregister_measurement_job_after_completion(
    job_id: str,
    generation: int,
    *,
    ownership_job_id: Optional[str] = None,
) -> None:
    services = _measurement_services()
    measurement_store = services.get_store()
    measurement_sr_session = services.get_session()
    logger.info("Measurement session job watcher started: job_id=%s generation=%s", job_id, generation)
    # The watcher is deliberately unbounded in wall-clock time: a registered
    # running job must never leave the measurement session merely because a
    # poll window elapsed, since a legitimate L/R repeat can outlive any
    # fixed window and the session owns the sample-rate/playback guards for
    # the whole job.  Terminality is guaranteed by the store lifecycle, so
    # the watcher ends on its own in every real outcome:
    # - a live worker always terminates the job (the runner terminalizes in
    #   every path and persists the terminal status);
    # - a stale/interrupted job without a live worker is promoted to
    #   "cancelled" by the existing store normalization, which the watcher
    #   observes through get_job() (resurrected records are promoted inside
    #   the store lookup itself);
    # - a session generation change ends the watcher immediately: the session
    #   was released and possibly restarted, so this watcher must neither
    #   keep polling nor unregister against the new session;
    # - the task dies with the event loop on shutdown.  The only remaining
    #   stop path is an explicit job cancel, which is the correct recovery
    #   for a wedged worker.
    while True:
        await asyncio.sleep(0.5)
        if measurement_sr_session is not None and measurement_sr_session.generation != generation:
            logger.info(
                "Measurement session job watcher stopped: job_id=%s session_generation=%s watcher_generation=%s",
                job_id,
                measurement_sr_session.generation,
                generation,
            )
            return
        if measurement_store is None:
            break
        try:
            job = measurement_store.get_job(job_id)
        except Exception as exc:
            logger.info("Measurement session job watcher stopped: job_id=%s job_lookup_failed=%s", job_id, exc)
            break
        status = str(job.get("status") or "")
        if status in {"completed", "failed", "cancelled"}:
            break
    if measurement_sr_session is not None and measurement_sr_session.generation == generation:
        await measurement_sr_session.unregister_manual_job(ownership_job_id or job_id)
    elif measurement_sr_session is not None:
        logger.info(
            "Measurement session stale job watcher ignored: job_id=%s watcher_generation=%s current_generation=%s",
            job_id,
            generation,
            measurement_sr_session.generation,
        )


def _normalize_measurement_optional_input_channel(value: Any) -> str:
    return normalize_measurement_optional_input_channel(value)


def _measurement_setup_settings_from_payload(settings: dict[str, Any]) -> dict[str, Any]:
    return measurement_setup_settings_from_payload(settings)


def _read_measurement_setup_settings() -> dict[str, Any]:
    measurement_store = _measurement_services().get_store()
    path = getattr(measurement_store, "settings_path", None)
    if not path:
        return _measurement_setup_settings_from_payload({})
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        settings = payload if isinstance(payload, dict) else {}
    except Exception:
        settings = {}
    return _measurement_setup_settings_from_payload(settings)


def _update_measurement_setup_settings(patch: dict[str, Any]) -> dict[str, Any]:
    measurement_store = _measurement_services().get_store()
    path = getattr(measurement_store, "settings_path", None)
    if not path:
        return _measurement_setup_settings_from_payload({})
    settings_path = Path(path)
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        settings = payload if isinstance(payload, dict) else {}
    except Exception:
        settings = {}
    measure_settings = settings.setdefault("measure", {})
    if not isinstance(measure_settings, dict):
        measure_settings = {}
        settings["measure"] = measure_settings

    if "selectedInputId" in patch or "input_id" in patch:
        measure_settings["selectedInputId"] = str(patch.get("selectedInputId", patch.get("input_id")) or "").strip()
    if "selectedInputKey" in patch or "input_key" in patch:
        measure_settings["selectedInputKey"] = str(patch.get("selectedInputKey", patch.get("input_key")) or "").strip()
    if "selectedMicInputChannel" in patch or "mic_input_channel" in patch:
        raw_mic = patch.get("selectedMicInputChannel", patch.get("mic_input_channel"))
        measure_settings["selectedMicInputChannel"] = _normalize_measurement_optional_input_channel(raw_mic) or "1"
    if "selectedReferenceInputChannel" in patch or "reference_input_channel" in patch:
        raw_reference = patch.get("selectedReferenceInputChannel", patch.get("reference_input_channel"))
        measure_settings["selectedReferenceInputChannel"] = _normalize_measurement_optional_input_channel(raw_reference)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return _measurement_setup_settings_from_payload(settings)


@router.get("/api/measurements")
async def list_measurements():
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    payload = measurement_store.list_measurements()
    payload["measurement_settings"] = _read_measurement_setup_settings()
    return payload


@router.get("/api/measurements/settings")
async def get_measurement_settings():
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    return {
        "status": "ok",
        "measurement_settings": _read_measurement_setup_settings(),
    }


@router.patch("/api/measurements/settings")
async def update_measurement_settings(request: Request):
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Measurement settings payload must be an object")
    return {
        "status": "ok",
        "measurement_settings": _update_measurement_setup_settings(body),
    }

@router.get("/api/measurements/inputs")
async def list_measurement_inputs():
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    return measurement_store.list_inputs()


@router.post("/api/measurements/calibrations")
async def upload_measurement_calibration(calibration_file: UploadFile = File(...)):
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    filename = calibration_file.filename or "calibration.txt"
    try:
        data = await read_upload(calibration_file, MEASUREMENT_TEXT_MAX_BYTES)
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    try:
        return measurement_store.upload_calibration_file(filename, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/measurements/calibrations/{calibration_id}/export")
async def export_measurement_calibration(calibration_id: str):
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        path, filename = measurement_store.get_calibration_file_for_export(calibration_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Calibration file not found")
    return FileResponse(path, filename=filename, media_type="text/plain")


@router.patch("/api/measurements/calibrations/active")
async def set_active_measurement_calibration(request: Request):
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    calibration_ref = payload.get("calibration_file_id") if isinstance(payload, dict) else ""
    return measurement_store.set_active_calibration_file_id(str(calibration_ref or ""))


@router.delete("/api/measurements/calibrations/{calibration_id}")
async def delete_measurement_calibration(calibration_id: str):
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        return measurement_store.delete_calibration_file(calibration_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Calibration file not found")


@router.post("/api/measurements/house-curves")
async def upload_measurement_house_curve(house_curve_file: UploadFile = File(...)):
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    filename = house_curve_file.filename or "house-curve.txt"
    try:
        data = await read_upload(house_curve_file, MEASUREMENT_TEXT_MAX_BYTES)
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    try:
        return measurement_store.upload_house_curve_file(filename, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/measurements/house-curves/{house_curve_id}/export")
async def export_measurement_house_curve(house_curve_id: str):
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        path, filename = measurement_store.get_house_curve_file_for_export(house_curve_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="House curve file not found")
    return FileResponse(path, filename=filename, media_type="text/plain")


@router.delete("/api/measurements/house-curves/{house_curve_id}")
async def delete_measurement_house_curve(house_curve_id: str):
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        return measurement_store.delete_house_curve_file(house_curve_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="House curve file not found")


@router.get("/api/measurements/{measurement_id}/file")
async def download_measurement_file(measurement_id: str):
    from main import (
        _path_within_root,
    )
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    measurement = next((item for item in measurement_store.list_measurements().get("measurements", []) if item.get("id") == measurement_id), None)
    if not measurement:
        raise HTTPException(status_code=404, detail="Measurement not found")
    storage_path = Path(str(measurement.get("storage_path") or "")).resolve()
    if not _path_within_root(storage_path, measurement_store.measurements_dir):
        raise HTTPException(status_code=403, detail="Measurement path outside measurement storage")
    if not storage_path.is_file():
        raise HTTPException(status_code=404, detail="Measurement file missing")
    return FileResponse(storage_path, filename=storage_path.name)

@router.get("/api/certificate/local-root")
async def download_local_root_certificate():
    cert_path = Path("/etc/fxroute/certs/fxroute-local-root.crt")
    if not cert_path.exists():
        raise HTTPException(status_code=404, detail="Local root certificate not available on this host")
    return FileResponse(cert_path, filename="fxroute-local-root.crt", media_type="application/x-x509-ca-cert")


async def _start_registered_manual_measurement(
    sample_rate: int,
    start_job: Callable[[], Awaitable[dict]],
    entry_epoch: int | None = None,
) -> dict:
    """Own pending session registration until a concrete job takes its place."""
    measurement_sr_session = _measurement_services().get_session()
    pending_job_id = f"pending:{uuid4()}"
    sweep_gen = measurement_sr_session.generation if measurement_sr_session is not None else 0
    pending_registered = False
    try:
        if measurement_sr_session is not None:
            sweep_gen = await measurement_sr_session.register_manual_job(
                pending_job_id, entry_epoch=entry_epoch
            )
            pending_registered = True
        await _measurement_entry_preflight(sample_rate)
        job = await start_job()
        if measurement_sr_session is not None:
            replacement = asyncio.create_task(
                measurement_sr_session.replace_manual_job(pending_job_id, job["id"])
            )
            try:
                await asyncio.shield(replacement)
            except BaseException:
                try:
                    await replacement
                except Exception:
                    # Keep the pending owner until the concrete job finishes if
                    # the atomic handoff itself failed.
                    asyncio.create_task(
                        _unregister_measurement_job_after_completion(
                            job["id"], sweep_gen, ownership_job_id=pending_job_id
                        )
                    )
                else:
                    asyncio.create_task(
                        _unregister_measurement_job_after_completion(job["id"], sweep_gen)
                    )
                pending_registered = False
                raise
            pending_registered = False
            asyncio.create_task(_unregister_measurement_job_after_completion(job["id"], sweep_gen))
        return job
    except BaseException:
        if pending_registered and measurement_sr_session is not None:
            cleanup = asyncio.create_task(
                measurement_sr_session.unregister_manual_job(pending_job_id)
            )
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # The cleanup task remains scheduled even when the request is cancelled.
                pass
        raise


@router.post("/api/measurements/start")
async def start_measurement(
    input_id: str = Form(...),
    input_key: str = Form(""),
    channel: str = Form("left"),
    mic_input_channel: str = Form("1"),
    reference_input_channel: str = Form(""),
    calibration_ref: str = Form(""),
    calibration_file: Optional[UploadFile] = File(None),
    measurement_role: str = Form(""),
):
    services = _measurement_services()
    measurement_store = services.get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    if services.auto_sub_active():
        raise HTTPException(status_code=423, detail="Auto Sub Optimize is in progress")

    measurement_sr_session = services.get_session()
    entry_epoch = (
        measurement_sr_session.capture_entry_epoch()
        if measurement_sr_session is not None
        else None
    )

    calibration_bytes = None
    calibration_filename = None
    if calibration_file is not None:
        calibration_filename = calibration_file.filename or "calibration.txt"
        try:
            calibration_bytes = await read_upload(calibration_file, MEASUREMENT_TEXT_MAX_BYTES)
        except UploadTooLargeError as e:
            raise HTTPException(status_code=413, detail=str(e))

    measurement_rate = _resolve_measurement_start_sample_rate()
    try:
        job = await _start_registered_manual_measurement(
            measurement_rate,
            lambda: measurement_store.start_measurement(
                input_id=input_id,
                input_key=input_key,
                channel=channel,
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                calibration_ref=calibration_ref,
                measurement_role=measurement_role,
            ),
            entry_epoch=entry_epoch,
        )
    except MeasurementEntryInvalidated:
        raise HTTPException(
            status_code=409,
            detail="Measurement start was cancelled because the measurement window was closed",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "ok", "job": job}

@router.post("/api/measurements/lr-repeat/start")
async def start_lr_repeat_measurement(
    input_id: str = Form(...),
    input_key: str = Form(""),
    base_name: str = Form(""),
    mic_input_channel: str = Form("1"),
    reference_input_channel: str = Form(""),
    calibration_ref: str = Form(""),
    calibration_file: Optional[UploadFile] = File(None),
):
    services = _measurement_services()
    measurement_store = services.get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    if services.auto_sub_active():
        raise HTTPException(status_code=423, detail="Auto Sub Optimize is in progress")

    measurement_sr_session = services.get_session()
    entry_epoch = (
        measurement_sr_session.capture_entry_epoch()
        if measurement_sr_session is not None
        else None
    )

    calibration_bytes = None
    calibration_filename = None
    if calibration_file is not None:
        calibration_filename = calibration_file.filename or "calibration.txt"
        try:
            calibration_bytes = await read_upload(calibration_file, MEASUREMENT_TEXT_MAX_BYTES)
        except UploadTooLargeError as e:
            raise HTTPException(status_code=413, detail=str(e))

    measurement_rate = _resolve_measurement_start_sample_rate()
    try:
        job = await _start_registered_manual_measurement(
            measurement_rate,
            lambda: measurement_store.start_lr_repeat_measurement(
                input_id=input_id,
                input_key=input_key,
                base_name=base_name,
                mic_input_channel=mic_input_channel,
                reference_input_channel=reference_input_channel,
                calibration_filename=calibration_filename,
                calibration_bytes=calibration_bytes,
                calibration_ref=calibration_ref,
            ),
            entry_epoch=entry_epoch,
        )
    except MeasurementEntryInvalidated:
        raise HTTPException(
            status_code=409,
            detail="Measurement start was cancelled because the measurement window was closed",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "ok", "job": job}

@router.get("/api/measurements/jobs/{job_id}")
async def get_measurement_job(job_id: str):
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        job = measurement_store.get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Measurement job not found")
    return {"status": "ok", "job": job}

@router.post("/api/measurements/jobs/{job_id}/cancel")
async def cancel_measurement_job(job_id: str):
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")
    try:
        job = measurement_store.cancel_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Measurement job not found")
    return {"status": "ok", "job": job}

@router.post("/api/measurements/save")
async def save_measurement(request: Request):
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")

    try:
        body = await request.json()
    except Exception:
        logger.exception("Measurement save request failed: invalid JSON body")
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    measurement_id = body.get("id") if isinstance(body, dict) else ""
    measurement_name = body.get("name") if isinstance(body, dict) else ""
    measurements = body.get("measurements") if isinstance(body, dict) else None
    trace_count = len(body.get("traces") or []) if isinstance(body, dict) and isinstance(body.get("traces"), list) else 0
    audio_output_context = _build_measurement_audio_output_context()
    if isinstance(body, dict) and not body.get("audio_output_context"):
        body["audio_output_context"] = audio_output_context

    logger.info(
        "Measurement save request received: id=%s name=%s traces=%s audio_output_mode=%s",
        measurement_id,
        measurement_name,
        trace_count,
        audio_output_context.get("output_mode", "unknown"),
    )
    try:
        if isinstance(measurements, list):
            for item in measurements:
                if isinstance(item, dict) and not item.get("audio_output_context"):
                    item["audio_output_context"] = audio_output_context
            saved_measurements = measurement_store.save_measurements(measurements)
            logger.info("Measurement set save completed: count=%s", len(saved_measurements))
            return {"status": "ok", "measurements": saved_measurements}
        saved = measurement_store.save_measurement(body)
    except ValueError as exc:
        logger.warning("Measurement save rejected: id=%s error=%s", measurement_id, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Measurement save failed: id=%s name=%s", measurement_id, measurement_name)
        raise
    logger.info(
        "Measurement save completed: id=%s name=%s",
        saved.get("id") if isinstance(saved, dict) else "",
        saved.get("name") if isinstance(saved, dict) else "",
    )
    return {"status": "ok", "measurement": saved}

@router.post("/api/measurements/merge")
async def merge_measurements(request: Request):
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    measurement_ids = body.get("measurementIds", body.get("measurement_ids")) if isinstance(body, dict) else []
    name = body.get("name") if isinstance(body, dict) else ""
    if not isinstance(measurement_ids, list):
        raise HTTPException(status_code=400, detail="measurementIds must be an array")

    try:
        merged = measurement_store.merge_measurements(measurement_ids, str(name or ""))
    except KeyError:
        raise HTTPException(status_code=404, detail="Measurement not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Measurement merge failed: ids=%s name=%s", measurement_ids, name)
        raise HTTPException(status_code=500, detail="Failed to merge selected measurements")
    return {"status": "ok", "measurement": merged}

@router.delete("/api/measurements/{measurement_id}")
async def delete_measurement(measurement_id: str):
    measurement_store = _measurement_services().get_store()
    if not measurement_store:
        raise HTTPException(status_code=503, detail="Measurement store not available")

    try:
        measurement_store.delete_measurement(measurement_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Measurement not found")
    return {"status": "ok", "deleted": measurement_id}
