# SPDX-License-Identifier: AGPL-3.0-only
"""Native DSP/output runtime orchestration extracted from main.py.

Owns the coordination between the native DSP runtime (:class:`dsp.runtime.DSPRuntime`),
the sample-rate overview, the subwoofer link watcher, and the peak monitor
refresh scheduling after DSP/effect changes.  It holds no application state
of its own: every dependency is injected explicitly through
:class:`DspOrchestrationDeps`, so it never imports ``main`` and main.py keeps
owning the runtime globals.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import playback.state as playback_state
import playback.source_policy as source_policy
import audio.samplerate as samplerate
from dsp.runtime import BassManagementConfig

logger = logging.getLogger(__name__)

# Bounds for the post-stale retry: wait at most this long for the sink rate to
# settle on the authoritative rate, then re-attempt the DSP sync with fresh
# live state (the stale overview that suppressed the first attempt is dropped).
STALE_SYNC_RETRY_DEADLINE_S = 30.0
STALE_SYNC_RETRY_POLL_S = 1.0

# Bounded wait for the hardware sink to renegotiate after a stale helper was
# rebuilt at the target rate (process rebuild, not a rate change).
STALE_HELPER_ALIGNMENT_TIMEOUT_MS = 2500


@dataclass(frozen=True)
class DspOrchestrationDeps:
    """Application services injected from main.py."""

    get_dsp_runtime: Callable[[], Any]
    get_dsp_manager: Callable[[], Any]
    get_audio_output_overview: Callable[[], dict]
    get_samplerate_status: Callable[[], dict]
    get_measurement_sr_session: Callable[[], Any]
    get_player_instance: Callable[[], Any]
    get_current_track_info: Callable[[], Any]
    get_peak_monitor: Callable[[], Any]
    peak_monitor_playback_armed: Callable[[], bool]
    set_peak_monitor_context_signature: Callable[[Any], None]
    get_spotify_ui_state: Callable[..., Awaitable[Any]]
    get_qobuz_ui_state: Callable[..., Awaitable[Any]]
    sync_peak_monitor_for_playback_state: Callable[..., Awaitable[Any]]
    sync_peak_monitor_for_spotify_state: Callable[..., Awaitable[Any]]
    sync_peak_monitor_for_qobuz_state: Callable[..., Awaitable[Any]]
    load_dsp_preset: Callable[..., Awaitable[Any]]
    broadcast: Callable[[dict], Awaitable[Any]]
    wait_for_samplerate_alignment: Callable[..., Awaitable[bool]]
    wait_for_selected_output_effective_rate: Callable[..., Awaitable[Any]]
    measurement_audio_graph_owned: Callable[[], bool]
    observe_playback_samplerate_drift: Callable[[], Awaitable[Any]]
    playback_transition_is_active: Callable[[], bool]
    coordinator_target_rate: Callable[..., Any]
    playback_graph_diagnosis: Callable[..., Awaitable[dict]]
    request_coordinated_recovery: Callable[..., Awaitable[Any]]
    create_lifecycle_background_task: Callable[..., Any]
    peak_monitor_restart_settle_ms: float
    sleep: Callable[[float], Awaitable[Any]]
    get_output_mode: Callable[[], str] | None = None
    # Live force-rate writer used only by the deliberate stale-helper repair to
    # drop a pin that contradicts the recovery target.
    set_pipewire_force_rate: Callable[[int], None] | None = None


def helper_argument_sample_rate(snapshot: dict | None) -> int | None:
    """Extract the native DSP rate from its runtime config."""
    config = (snapshot or {}).get("config")
    if isinstance(config, dict) and isinstance(config.get("sample_rate"), int):
        return config["sample_rate"]
    return None


def with_subwoofer_derived_delays(overview: dict) -> dict:
    output_mode = overview.get("output_mode") or {}
    if output_mode.get("mode") in samplerate.OUTPUT_MODE_SUBWOOFER_22_MODES:
        config = BassManagementConfig.from_overview(overview)
        overview["output_mode"] = {
            **output_mode,
            "derived_main_delay_ms": config.derived_main_delay_ms,
            "derived_sub1_delay_ms": config.derived_sub1_delay_ms,
            "derived_sub2_delay_ms": config.derived_sub2_delay_ms,
        }
    return overview


class DspOrchestrator:
    """Coordinate native DSP runtime sync, link watching, and peak refresh."""

    def __init__(self, deps: DspOrchestrationDeps, *, stale_retry_deadline_s: float = STALE_SYNC_RETRY_DEADLINE_S):
        self._deps = deps
        self._stale_retry_deadline_s = stale_retry_deadline_s

    async def sync_runtime(
        self,
        audio_overview: dict | None = None,
        *,
        reason: str = "unspecified",
        _rate_lock_held: bool = False,
        target_overview: dict | None = None,
        retry_on_stale: bool = False,
        allow_unsettled_rate: bool = False,
    ) -> dict:
        """Synchronize the native helper from one live, lock-protected rate.

        A caller-supplied ``audio_overview`` is only a stale-check token: it
        contributes the requested rate the stale guard compares against the
        live authoritative rate.  The helper config itself is rebuilt from an
        explicit transition target (``target_overview``) or, otherwise, from
        the live overview re-read under the lock after the rate gates, so a
        delayed caller can never restart a helper with its old selection or
        mode at the same rate.

        When ``retry_on_stale`` is set and the stale-check suppresses the
        restart because the caller's requested rate does not match the live
        authoritative rate, a bounded background task re-attempts the sync once
        the sink settles on the authoritative rate. This closes the gap where a
        user-initiated output switch during rate-pinned playback persisted the
        selection but left the graph linked to the previous card forever.

        ``allow_unsettled_rate`` is reserved for a deliberate stale-helper
        repair (see :meth:`recover_stale_helper_samplerate`): the caller's
        explicit target becomes authoritative and the sink-rate gates are
        bypassed once, because a stale helper pins the hardware sink away from
        that target and the gates can therefore never pass.
        """
        overview_was_supplied = audio_overview is not None
        overview = (
            audio_overview
            if audio_overview
            else await asyncio.to_thread(self._deps.get_audio_output_overview)
        )
        dsp_runtime = self._deps.get_dsp_runtime()

        if dsp_runtime is None:
            return overview

        requested_rate = samplerate.overview_sample_rate(overview) if overview_was_supplied else None

        async def _sync_locked() -> dict:
            try:
                # Bounded PipeWire subprocess pipeline; run it off the event loop.
                samplerate_status = await asyncio.to_thread(
                    self._deps.get_samplerate_status
                )
            except Exception as exc:
                logger.warning(
                    "Subwoofer runtime sync skipped: authoritative samplerate unavailable reason=%s error=%s",
                    reason, exc,
                )
                return overview

            authoritative_rate = samplerate.authoritative_sample_rate(samplerate_status)
            if authoritative_rate is None:
                logger.warning(
                    "Subwoofer runtime sync skipped: authoritative samplerate missing reason=%s requested_rate=%s",
                    reason, requested_rate,
                )
                return overview

            if allow_unsettled_rate and requested_rate is not None:
                # Deliberate stale-helper repair: the requested target is
                # authoritative.  The gates below require the hardware sink to
                # already sit at that rate -- exactly what the stale helper
                # prevents -- so they can never pass and are bypassed once.
                repair_overview = samplerate.audio_output_overview_with_effective_rate(
                    target_overview or overview, requested_rate,
                )
                await dsp_runtime.sync(repair_overview)
                return repair_overview

            sink_rate = samplerate_status.get("active_rate")
            if sink_rate != authoritative_rate:
                logger.info(
                    "Subwoofer runtime sync deferred until sink reaches authoritative rate: "
                    "reason=%s requested_rate=%s authoritative_rate=%s hardware_sink_rate=%s",
                    reason, requested_rate, authoritative_rate, sink_rate,
                )
                return overview

            if requested_rate is not None and requested_rate != authoritative_rate:
                logger.info(
                    "Native DSP sync stale; restart suppressed: reason=%s requested_rate=%s authoritative_rate=%s",
                    reason, requested_rate, authoritative_rate,
                )
                if retry_on_stale:
                    self._deps.create_lifecycle_background_task(
                        self._sync_after_stale_settle(
                            reason=reason,
                            requested_rate=requested_rate,
                        ),
                        name=f"dsp-sync-retry:{reason}",
                    )
                return overview
            if target_overview is not None:
                # The caller carries an explicit transition target (e.g. the
                # measurement entry rebuilding the helper at the target rate
                # before the hardware has moved there).
                current_overview = target_overview
            else:
                # The caller overview is only a stale-check token: rebuild
                # from the live state read now, after the rate gates passed.
                try:
                    current_overview = await asyncio.to_thread(
                        self._deps.get_audio_output_overview
                    )
                except Exception as exc:
                    logger.warning(
                        "Native DSP sync could not re-read the live overview; "
                        "falling back to the caller overview reason=%s error=%s",
                        reason, exc,
                    )
                    current_overview = audio_overview or overview or {}
            current_overview = samplerate.audio_output_overview_with_effective_rate(
                current_overview, authoritative_rate,
            )
            pre_start_status = await asyncio.to_thread(
                self._deps.get_samplerate_status
            )
            pre_start_rate = samplerate.authoritative_sample_rate(pre_start_status)
            pre_start_sink_rate = pre_start_status.get("active_rate")
            if pre_start_rate != authoritative_rate or pre_start_sink_rate != authoritative_rate:
                logger.info(
                    "Subwoofer runtime sync stale immediately before helper start; restart suppressed: "
                    "reason=%s requested_rate=%s authoritative_rate=%s pre_start_rate=%s pre_start_sink_rate=%s",
                    reason, requested_rate, authoritative_rate, pre_start_rate, pre_start_sink_rate,
                )
                return current_overview
            current_overview = samplerate.audio_output_overview_with_effective_rate(
                current_overview, pre_start_rate,
            )
            final_status = await asyncio.to_thread(self._deps.get_samplerate_status)
            final_rate = samplerate.authoritative_sample_rate(final_status)
            if final_rate != authoritative_rate:
                logger.info(
                    "Native DSP sync stale at start gate; restart suppressed: "
                    "reason=%s requested_rate=%s expected_rate=%s final_rate=%s",
                    reason, requested_rate, authoritative_rate, final_rate,
                )
                return current_overview
            await dsp_runtime.sync(current_overview)
            return current_overview

        measurement_sr_session = self._deps.get_measurement_sr_session()
        if _rate_lock_held or measurement_sr_session is None:
            return await _sync_locked()
        async with measurement_sr_session.lock:
            return await _sync_locked()

    async def _sync_after_stale_settle(
        self,
        *,
        reason: str,
        requested_rate: int,
    ) -> None:
        """Re-attempt a suppressed DSP sync once the sink rate settles.

        The original overview is deliberately dropped: it carried the stale
        requested rate. The retry reads the live overview again, so the helper
        is rebuilt from current state at whatever rate the graph settled on.
        """
        deadline = time.monotonic() + self._stale_retry_deadline_s
        while time.monotonic() <= deadline:
            try:
                samplerate_status = await asyncio.to_thread(
                    self._deps.get_samplerate_status
                )
            except Exception as exc:
                logger.warning(
                    "DSP sync retry skipped: authoritative samplerate unavailable reason=%s error=%s",
                    reason, exc,
                )
                return
            authoritative_rate = samplerate.authoritative_sample_rate(samplerate_status)
            sink_rate = samplerate_status.get("active_rate")
            if (
                isinstance(authoritative_rate, int)
                and authoritative_rate > 0
                and sink_rate == authoritative_rate
            ):
                logger.info(
                    "DSP sync retry after stale suppression: reason=%s requested_rate=%s settled_rate=%s",
                    reason, requested_rate, authoritative_rate,
                )
                # No overview is passed: the fresh live state decides the rate,
                # so the stale branch (which compares a caller-provided requested
                # rate) cannot re-trigger and this retry runs exactly once.
                await self.sync_runtime(reason=f"{reason}-retry-after-stale")
                return
            await self._deps.sleep(STALE_SYNC_RETRY_POLL_S)
        logger.warning(
            "DSP sync retry gave up: sink never settled on authoritative rate reason=%s requested_rate=%s",
            reason, requested_rate,
        )

    async def recover_stale_helper_samplerate(
        self,
        target_rate: int,
        *,
        reason: str = "unspecified",
        _rate_lock_held: bool = False,
    ) -> bool:
        """Rebuild a stale native DSP helper so the hardware sink can move.

        A helper left running at a previous rate keeps the hardware sink at
        that rate: ``clock.force-rate`` writes, sink suspend/resume pulses and
        the idle silent trigger all fail to renegotiate a sink that an active
        helper stream clocks.  Rebuilding the helper at ``target_rate`` is the
        only path that releases that pin, so this is the recovery for the
        circular helper/sink state a skipped or deferred measurement restore
        can leave behind.

        Only that exact state is touched: the helper must be live at a rate
        other than the target and the sink must not already be aligned.
        Returns whether the sink ended up aligned at ``target_rate``.
        """
        if not isinstance(target_rate, int) or target_rate <= 0:
            return False
        if self._deps.measurement_audio_graph_owned():
            return False
        dsp_runtime = self._deps.get_dsp_runtime()
        if dsp_runtime is None:
            return False
        try:
            status = dict(await asyncio.to_thread(self._deps.get_samplerate_status))
        except Exception:
            return False
        if samplerate.playback_rate_aligned(status, target_rate):
            return True
        try:
            snapshot = dict(dsp_runtime.snapshot() or {})
        except Exception:
            return False
        helper_rate = helper_argument_sample_rate(snapshot)
        if not snapshot.get("active") or helper_rate is None or helper_rate == target_rate:
            # No live helper, or it already runs at the target: not this state.
            return False
        force_rate = status.get("force_rate")
        if (
            self._deps.set_pipewire_force_rate is not None
            and isinstance(force_rate, int)
            and force_rate > 0
            and force_rate != target_rate
        ):
            # A pin that contradicts the target would leave the graph resampling
            # to the stale rate even after the helper rebuild, so the recovery
            # owns the pin too (same intent: the caller's target rate).
            try:
                await asyncio.to_thread(self._deps.set_pipewire_force_rate, target_rate)
            except Exception as exc:
                logger.warning(
                    "Stale native DSP helper rebuild could not align the force-rate pin: "
                    "reason=%s target_rate=%s pinned_rate=%s error=%s",
                    reason, target_rate, force_rate, exc,
                )
                return False
        try:
            overview = await asyncio.to_thread(self._deps.get_audio_output_overview)
        except Exception:
            return False
        staged_overview = samplerate.audio_output_overview_with_effective_rate(
            overview, target_rate,
        )
        logger.warning(
            "Stale native DSP helper rebuild: reason=%s target_rate=%s helper_rate=%s hardware_sink_rate=%s",
            reason, target_rate, helper_rate, status.get("active_rate"),
        )
        try:
            await self.sync_runtime(
                audio_overview=staged_overview,
                target_overview=staged_overview,
                reason=f"stale-helper-rebuild:{reason}",
                _rate_lock_held=_rate_lock_held,
                allow_unsettled_rate=True,
            )
        except Exception as exc:
            logger.error(
                "Stale native DSP helper rebuild failed: reason=%s target_rate=%s error=%s",
                reason, target_rate, exc,
            )
            return False
        aligned = bool(await self._deps.wait_for_samplerate_alignment(
            target_rate, timeout_ms=STALE_HELPER_ALIGNMENT_TIMEOUT_MS,
        ))
        logger.info(
            "Stale native DSP helper rebuild result: reason=%s target_rate=%s aligned=%s helper_rate_before=%s",
            reason, target_rate, aligned, helper_rate,
        )
        return aligned

    async def _repair_idle_stale_pinned_rate(self, dsp_runtime: Any) -> None:
        """Repair the one unambiguous idle helper/rate inconsistency.

        With no current track the link watcher has no source identity or
        target rate, so the full link recovery cannot run.  A live force-rate
        pin the running helper does not honour is still unambiguous: the
        helper holds the hardware sink at its own rate, so every later play on
        the pinned rate would fail its target-rate stage.  Only that state is
        repaired; a healthy idle graph is left untouched.
        """
        try:
            status = dict(await asyncio.to_thread(self._deps.get_samplerate_status))
        except Exception:
            return
        pinned_rate = status.get("force_rate")
        if not isinstance(pinned_rate, int) or pinned_rate <= 0:
            return
        if samplerate.playback_rate_aligned(status, pinned_rate):
            return
        try:
            snapshot = dict(dsp_runtime.snapshot() or {})
        except Exception:
            return
        helper_rate = helper_argument_sample_rate(snapshot)
        if not snapshot.get("active") or helper_rate is None or helper_rate == pinned_rate:
            return
        logger.warning(
            "Idle stale native DSP helper detected: pinned_rate=%s helper_rate=%s hardware_sink_rate=%s",
            pinned_rate, helper_rate, status.get("active_rate"),
        )
        await self.recover_stale_helper_samplerate(
            pinned_rate, reason="idle-helper-rate-watch",
        )

    async def sync_runtime_at_rate(self, target_rate: int, *, _rate_lock_held: bool = False) -> None:
        """Re-sync through the central live-rate helper path after a rate transition."""
        dsp_runtime = self._deps.get_dsp_runtime()
        if dsp_runtime is None:
            logger.info(
                "Subwoofer runtime measurement release re-sync skipped: dsp_runtime_missing=true target_rate=%s",
                target_rate,
            )
            return
        logger.info("Subwoofer runtime measurement release re-sync requested: raw_target_rate=%s", target_rate)

        # The native DSP runtime owns Stereo as well as subwoofer modes, so a
        # measurement release re-syncs it at the restore rate for every mode: the
        # guarded measurement entry may have rebuilt it at the measurement rate
        # (e.g. the 48 kHz sweep path).  target_rate is diagnostic/stale-check
        # information only. Do not inject it into an overview: the central sync
        # reads the authoritative rate again under the sample-rate lock
        # immediately before deciding to start.
        if target_rate > 0:
            selected_aligned, _ = await self._deps.wait_for_selected_output_effective_rate(target_rate, timeout_ms=3500)
            sink_aligned = await self._deps.wait_for_samplerate_alignment(target_rate, timeout_ms=3500)
            if not selected_aligned or not sink_aligned:
                # A measurement release must not leave an ownerless graph at
                # the measurement rate.  The sink cannot reach the restore
                # rate while a stale helper still clocks it, so repair that
                # helper instead of deferring the restore forever (the skipped
                # ``intent-changed-after-quiet`` restore path used to stop
                # here and persist a 48 kHz helper under a 44.1 kHz pin).
                recovered = await self.recover_stale_helper_samplerate(
                    target_rate,
                    reason="measurement-release",
                    _rate_lock_held=_rate_lock_held,
                )
                if not recovered:
                    logger.warning(
                        "Subwoofer runtime measurement release re-sync deferred: target_rate=%s "
                        "selected_output_aligned=%s sink_aligned=%s",
                        target_rate, selected_aligned, sink_aligned,
                    )
                    return
        await self.sync_runtime(
            reason="measurement-release", _rate_lock_held=_rate_lock_held,
        )
        await self._deps.sleep(0.5)
        await self.sync_runtime(
            reason="measurement-release-settle", _rate_lock_held=_rate_lock_held,
        )
        runtime_snapshot = dsp_runtime.snapshot()
        try:
            samplerate_status = await asyncio.to_thread(
                self._deps.get_samplerate_status
            )
        except Exception:
            samplerate_status = {}
        logger.info(
            "Subwoofer runtime measurement release re-sync verified: target_rate=%s authoritative_rate=%s "
            "hardware_sink_rate=%s active=%s helper_pid=%s helper_rate=%s",
            target_rate,
            samplerate.authoritative_sample_rate(samplerate_status),
            samplerate_status.get("active_rate") if isinstance(samplerate_status, dict) else None,
            runtime_snapshot.get("active"),
            runtime_snapshot.get("helper_pid"),
            helper_argument_sample_rate(runtime_snapshot),
        )

    async def sync_preset_for_playback_samplerate(
        self,
        *,
        sample_rate_hz: int | None,
        reason: str,
        detail: str = "",
        _rate_lock_held: bool = False,
    ) -> None:
        dsp_manager = self._deps.get_dsp_manager()
        if not dsp_manager or not isinstance(sample_rate_hz, int) or sample_rate_hz <= 0:
            return

        active_preset = dsp_manager.get_active_preset()
        if not active_preset or active_preset in dsp_manager.EXCLUDED_GLOBAL_EXTRAS_PRESETS:
            return

        logger.info(
            "Syncing DSP preset for playback samplerate: preset=%s sample_rate=%s reason=%s detail=%s",
            active_preset,
            sample_rate_hz,
            reason,
            detail,
        )
        await self._deps.load_dsp_preset(
            active_preset,
            convolver_sample_rate_hz=sample_rate_hz,
            _rate_lock_held=_rate_lock_held,
        )
        status = dsp_manager.get_status()
        await self._deps.broadcast({"type": "dsp", "data": status})

    @staticmethod
    def _is_topology_complete_rate_only_mismatch(diagnosis: dict) -> bool:
        """Return whether only the helper rate mismatches an intact topology.

        All links present, source links complete, no direct-to-hardware
        bypass, DSP/helper ports up: the graph is canonically linked and the
        ``links_complete=False`` verdict rests solely on the track-derived
        rate expectation. Rate ownership belongs to the drift observer.
        """
        links = diagnosis.get("links") or {}
        if not isinstance(links, dict) or not links or not all(links.values()):
            return False
        return bool(
            diagnosis.get("source_links_complete") is True
            and not diagnosis.get("direct_source_to_hw_present")
            and diagnosis.get("dsp_ports") is True
            and diagnosis.get("helper_active") is True
            and diagnosis.get("helper_rate_matches") is False
        )

    async def runtime_link_watch_loop(self) -> None:
        while True:
            await self._deps.sleep(2.0)
            try:
                if self._deps.measurement_audio_graph_owned():
                    logger.debug("Subwoofer link watcher skipped while Measurement owns the audio graph")
                    continue
                await self._deps.observe_playback_samplerate_drift()
                dsp_runtime = self._deps.get_dsp_runtime()
                if dsp_runtime is None:
                    continue
                mode_provider = self._deps.get_output_mode
                if mode_provider is not None and str(mode_provider() or "stereo") not in samplerate.OUTPUT_MODE_SUBWOOFER_MODES:
                    # Stereo output needs no subwoofer link watch; skip the
                    # overview build (dozens of short-lived PipeWire/BlueZ
                    # subprocesses per tick) until a subwoofer mode is active.
                    continue
                # The overview build spawns a dozen PipeWire/BlueZ subprocesses;
                # keep that blocking pipeline off the event loop so playback
                # transitions, IPC, and status endpoints stay responsive.
                overview = await asyncio.to_thread(self._deps.get_audio_output_overview)
                output_mode = overview.get("output_mode") or {}
                if output_mode.get("mode") not in samplerate.OUTPUT_MODE_SUBWOOFER_MODES:
                    continue
                if self._deps.playback_transition_is_active():
                    continue
                if dsp_runtime.sync_in_progress:
                    logger.debug(
                        "Subwoofer link watcher skipped while a subwoofer runtime reconfiguration is in progress"
                    )
                    continue
                track = dict(self._deps.get_current_track_info() or {})
                if not track:
                    # No source owns a target rate.  Repair a pinned helper
                    # that does not honour the live force-rate pin rather than
                    # preserving that inconsistency until the next play fails.
                    await self._repair_idle_stale_pinned_rate(dsp_runtime)
                    continue
                source = str(track.get("source") or "")
                target_rate = self._deps.coordinator_target_rate(source, track)
                diagnosis = await self._deps.playback_graph_diagnosis(
                    overview,
                    source=source,
                    target_rate=target_rate,
                    require_source=True,
                )
                if diagnosis.get("links_complete"):
                    continue
                if source_policy.is_mpv_source(source) and self._is_topology_complete_rate_only_mismatch(diagnosis):
                    # Pure helper-rate mismatch with complete topology: the
                    # track-derived target may be stale (e.g. a radio track
                    # still carrying a previous fixed rate while live MPV
                    # already runs the auto rate). The drift observer owns
                    # rate mismatches with live-MPV authority and stable
                    # readbacks; a full link-watcher recovery here would flap
                    # the hardware rate.
                    logger.debug(
                        "Subwoofer link watcher deferring pure rate mismatch to drift observer: "
                        "source=%s helper_rate=%s",
                        source,
                        diagnosis.get("helper_rate"),
                    )
                    continue
                logger.info(
                    "Subwoofer link watcher observed incomplete canonical graph; requesting Coordinator action: "
                    "bypass_only=%s helper_active=%s helper_rate=%s direct_source_to_hw=%s signature=%s",
                    diagnosis.get("bypass_only"),
                    diagnosis.get("helper_active"),
                    diagnosis.get("helper_rate"),
                    diagnosis.get("direct_source_to_hw_present"),
                    diagnosis.get("signature"),
                )
                await self._deps.request_coordinated_recovery(
                    track,
                    "subwoofer-link-watcher",
                    graph_only=bool(diagnosis.get("bypass_only")),
                    diagnosis=diagnosis,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Subwoofer link watch repair failed: %s", exc)

    async def refresh_peak_monitor_after_effects_change(self, reason: str = "effects-change") -> None:
        peak_monitor = self._deps.get_peak_monitor()
        if not peak_monitor or not self._deps.peak_monitor_playback_armed():
            return

        player_instance = self._deps.get_player_instance()
        player_state = player_instance.state if player_instance else {}
        spotify_state = await self._deps.get_spotify_ui_state()
        qobuz_state = await self._deps.get_qobuz_ui_state()
        is_local_playing = playback_state.is_local_playback_active(player_state)
        is_spotify_playing = playback_state.is_external_playback_active(spotify_state)
        is_qobuz_playing = playback_state.is_external_playback_active(qobuz_state)

        if not is_local_playing and not is_spotify_playing and not is_qobuz_playing:
            return

        logger.info("Refreshing peak monitor after %s", reason)
        self._deps.set_peak_monitor_context_signature(None)
        await self._deps.sleep(self._deps.peak_monitor_restart_settle_ms / 1000)

        if is_spotify_playing:
            await self._deps.sync_peak_monitor_for_spotify_state(spotify_state)
        elif is_qobuz_playing:
            await self._deps.sync_peak_monitor_for_qobuz_state(qobuz_state)
        elif is_local_playing:
            await self._deps.sync_peak_monitor_for_playback_state(player_state)

    async def _run_peak_monitor_refresh_after_effects_change(self, reason: str, timeout: float = 4.0) -> None:
        try:
            await asyncio.wait_for(self.refresh_peak_monitor_after_effects_change(reason), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Timed out refreshing peak monitor after %s", reason)
        except Exception as e:
            logger.warning("Failed refreshing peak monitor after %s: %s", reason, e)

    def schedule_peak_monitor_refresh_after_effects_change(self, reason: str = "effects-change") -> None:
        self._deps.create_lifecycle_background_task(
            self._run_peak_monitor_refresh_after_effects_change(reason),
            name=f"peak-monitor-refresh:{reason}",
        )
