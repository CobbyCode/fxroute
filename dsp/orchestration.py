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
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import playback.state as playback_state
import audio.samplerate as samplerate
from dsp.runtime import BassManagementConfig

logger = logging.getLogger(__name__)


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
    sync_peak_monitor_for_playback_state: Callable[..., Awaitable[Any]]
    sync_peak_monitor_for_spotify_state: Callable[..., Awaitable[Any]]
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

    def __init__(self, deps: DspOrchestrationDeps):
        self._deps = deps

    async def sync_runtime(
        self,
        audio_overview: dict | None = None,
        *,
        reason: str = "unspecified",
        _rate_lock_held: bool = False,
        target_overview: dict | None = None,
    ) -> dict:
        """Synchronize the native helper from one live, lock-protected rate.

        An overview passed by a transition/release caller is only a stale-check
        token. The actual helper config is rebuilt after the final live PipeWire
        read, so a delayed caller cannot restart a helper with its old target.
        """
        overview_was_supplied = audio_overview is not None
        overview = audio_overview or self._deps.get_audio_output_overview()
        dsp_runtime = self._deps.get_dsp_runtime()

        if dsp_runtime is None:
            return overview

        requested_rate = samplerate.overview_sample_rate(overview) if overview_was_supplied else None

        async def _sync_locked() -> dict:
            try:
                samplerate_status = self._deps.get_samplerate_status()
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

            sink_rate = samplerate_status.get("active_rate")
            if sink_rate != authoritative_rate:
                logger.info(
                    "Subwoofer runtime sync deferred until sink reaches authoritative rate: "
                    "reason=%s requested_rate=%s authoritative_rate=%s hardware_sink_rate=%s",
                    reason, requested_rate, authoritative_rate, sink_rate,
                )
                return overview

            current_overview = target_overview or audio_overview or self._deps.get_audio_output_overview()
            if requested_rate is not None and requested_rate != authoritative_rate:
                logger.info(
                    "Native DSP sync stale; restart suppressed: reason=%s requested_rate=%s authoritative_rate=%s",
                    reason, requested_rate, authoritative_rate,
                )
                return overview
            current_overview = samplerate.audio_output_overview_with_effective_rate(
                current_overview, authoritative_rate,
            )
            pre_start_status = self._deps.get_samplerate_status()
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
            final_status = self._deps.get_samplerate_status()
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
            samplerate_status = self._deps.get_samplerate_status()
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
                overview = self._deps.get_audio_output_overview()
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
                logger.info(
                    "Subwoofer link watcher observed incomplete canonical graph; requesting Coordinator action: "
                    "bypass_only=%s helper_active=%s helper_rate=%s direct_bypass=%s signature=%s",
                    diagnosis.get("bypass_only"),
                    diagnosis.get("helper_active"),
                    diagnosis.get("helper_rate"),
                    diagnosis.get("direct_ee_to_hw_present"),
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
        is_local_playing = playback_state.is_local_playback_active(player_state)
        is_spotify_playing = bool(spotify_state.get("available") and spotify_state.get("status") == "Playing")

        if not is_local_playing and not is_spotify_playing:
            return

        logger.info("Refreshing peak monitor after %s", reason)
        self._deps.set_peak_monitor_context_signature(None)
        await self._deps.sleep(self._deps.peak_monitor_restart_settle_ms / 1000)

        if is_spotify_playing:
            await self._deps.sync_peak_monitor_for_spotify_state(spotify_state)
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
