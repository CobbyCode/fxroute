# SPDX-License-Identifier: AGPL-3.0-only

"""Snapshot, abort, and committed-source restore operations of the adapter.

Extracted from :class:`FxrouteTransitionRuntime`; runs on the composing
adapter instance and reads the attributes declared on the class below.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Mapping

import audio.samplerate as samplerate
from audio.samplerate import OUTPUT_MODE_STEREO, OUTPUT_MODE_SUBWOOFER_MODES
from playback.transition import TransitionRequest

from .deps import PlaybackRuntimeDependencies

logger = logging.getLogger(__name__)


class _RuntimeSnapshotMixin:
    """Attributes provided by the composing adapter instance."""
    _deps: PlaybackRuntimeDependencies
    _staged_target_url: str | None

    async def read_measurement_session_graph(self, target_rate: int) -> dict[str, Any]:
        """Read the active-measurement graph without touching playback state."""
        rate = dict(self._deps.get_samplerate_status())
        diagnosis = await self._deps.playback_graph_diagnosis(
            target_rate=target_rate,
            require_source=False,
        )
        result = dict(diagnosis)
        result["active_rate"] = rate.get("active_rate")
        result["force_rate"] = rate.get("force_rate")
        result["measurement_rate_aligned"] = bool(
            samplerate.playback_rate_aligned(rate, target_rate)
        )
        result["repairable_link_loss"] = self._deps.measurement_session_link_loss_is_repairable(
            result,
            target_rate=target_rate,
        )
        return result

    async def reconcile_measurement_session_graph(self, _target_rate: int) -> None:
        """Repair only existing production links; never reload EE or the helper."""
        diagnosis = await self._deps.playback_graph_diagnosis(
            target_rate=_target_rate,
            require_source=False,
        )
        if diagnosis.get("mode") in OUTPUT_MODE_SUBWOOFER_MODES:
            await self._deps.coordinator_reconcile_subwoofer_links_only()
        elif diagnosis.get("mode") == OUTPUT_MODE_STEREO:
            await self._deps.repair_stereo_output_links_once(diagnosis)
        else:
            raise RuntimeError(
                "measurement session graph reconciliation has no mode repair path"
            )

    async def read_transition_snapshot(self, request: TransitionRequest) -> dict[str, Any]:
        self._staged_target_url = None
        state = dict(self._player.state if self._player else {})
        try:
            rate = dict(self._deps.get_samplerate_status())
        except Exception:
            rate = {}
        snapshot = {
            "player": state,
            "active_rate": rate.get("active_rate"),
            "force_rate": rate.get("force_rate"),
            "source": request.source,
            "target_url": request.target_url,
            "current_track": dict(self._deps.get_current_track_info() or {}),
            "playback_intent_generation": self._deps.get_playback_intent_generation(),
        }
        if request.operation == "output-mode-switch":
            snapshot["output_mode_overview"] = copy.deepcopy(self._deps.get_audio_output_overview())
            snapshot["output_mode_config"] = copy.deepcopy(
                samplerate._load_raw_audio_output_mode()
            )
            snapshot["ee_active_preset"] = (
                self._dsp_manager.get_active_preset()
                if self._dsp_manager is not None
                else None
            )
            snapshot["spotify"] = await self._deps.get_spotify_ui_state()
        else:
            # Frozen once per transition; the graph-diagnosis stages reuse it
            # instead of re-running the full pactl/pw-cli output enumeration.
            snapshot["audio_overview"] = copy.deepcopy(self._deps.get_audio_output_overview())
        return snapshot

    def target_source_staged(self, request: TransitionRequest) -> bool:
        """Report whether this transition has staged a new MPV target."""
        return bool(
            request.source in {"local", "radio"}
            and request.target_url
            and self._staged_target_url == request.target_url
        )

    async def wait_for_pipewire_spotify_release(self) -> bool:
        """Quiesce an active Spotify sink input within the bounded release."""
        return bool(await self._deps.wait_for_pipewire_spotify_release())

    async def abort_failed_transition(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any] | None,
        *,
        target_staged: bool,
    ) -> dict[str, Any] | None:
        """Decide the failed-handoff outcome and perform primitive cleanup.

        The Coordinator has already attenuated and paused the source before it
        calls this hook.  Returns None when the committed context is unchanged
        (MPV still exposes the exact pre-transition file): nothing is
        invalidated and no restore runs.  Returns ``{"restore": <request>}``
        when the previously committed Local/Radio source must be physically
        restored; the Coordinator then runs that request through its own
        transition stages under the still-closed output gate.  Returns
        ``{"invalidate": True}`` after stopping a staged target and
        invalidating only the active track metadata, while preserving
        ``last_track_info`` and the committed queue state: a failed transition
        must never discard the previously working queue.
        """
        snapshot_track = dict((snapshot or {}).get("current_track") or {})
        previous_state = dict((snapshot or {}).get("player") or {})
        if request.source not in {"local", "radio"}:
            if request.source != "spotify":
                return None
            # A failed Spotify handoff already quieted and stopped the
            # previously committed Local/Radio source and cleared its track
            # context before the Spotify start was verified (quiet_old_source
            # -> self._deps.pause_local_playback_for_spotify_broadcast).  Ask
            # the Coordinator to restore the pre-transition committed source
            # physically (sample rate, MPV load, pause/play state, volume) so
            # a failed Spotify start never loses both sources.  The committed
            # queue, last_track_info and radio-reconnect state were never
            # touched by the handoff and stay as they are.  On success the
            # Coordinator restores the output gate instead of latching a
            # failure; on restore failure the existing failure latch keeps
            # the safe state.
            if snapshot_track.get("source") in {"local", "radio"} and bool(
                previous_state.get("current_file")
                or previous_state.get("playing")
                or previous_state.get("paused")
                or previous_state.get("ended")
            ):
                restore_request = self._build_restore_request(
                    request, snapshot, previous_state, snapshot_track
                )
                if restore_request is not None:
                    return {"restore": restore_request}
                logger.warning(
                    "Spotify handoff failed and the committed %s source could not be "
                    "restored; keeping the failure gate latched: track_id=%s url=%s",
                    snapshot_track.get("source"),
                    snapshot_track.get("id"),
                    snapshot_track.get("url"),
                )
            return None

        current_state = dict(self._player.state if self._player else {})
        previous_file = previous_state.get("current_file")
        current_file = current_state.get("current_file")
        previous_context_unchanged = (
            not target_staged
            and current_file == previous_file
            and not (current_file is None and self._deps.get_current_track_info())
        )

        if previous_context_unchanged:
            live_track = self._deps.get_current_track_info() or {}
            if current_file and live_track.get("url") not in {None, current_file}:
                if snapshot_track.get("url") == current_file:
                    self._deps.set_current_track_info(snapshot_track)
                else:
                    previous_context_unchanged = False
            if previous_context_unchanged:
                # The committed queue and track context stay valid: MPV still
                # exposes the exact pre-transition file and a staged queue
                # candidate was never published.  Nothing to invalidate.
                return None

        if snapshot_track.get("source") in {"local", "radio"} and previous_state.get("current_file"):
            restore_request = self._build_restore_request(
                request, snapshot, previous_state, snapshot_track
            )
            if restore_request is not None:
                return {"restore": restore_request}

        # The target was staged, the old file disappeared, or the active
        # metadata no longer matches MPV. Stop the physical target first and
        # then invalidate only the active context. last_track_info is
        # deliberately untouched so the caller can offer a retry.  The
        # committed queue state is preserved.
        self._stop_staged_target_and_invalidate()
        return {"invalidate": True}

    def _stop_staged_target_and_invalidate(self) -> None:
        """Stop a staged MPV target and invalidate only the active context."""
        if self._deps.player_is_running():
            try:
                set_volume = getattr(self._player, "set_volume", None)
                if callable(set_volume):
                    set_volume(0)
            except Exception:
                logger.warning(
                    "Failed to attenuate MPV during failed transition abort",
                    exc_info=True,
                )
            try:
                stop_playback = getattr(self._player, "stop_playback", None)
                if callable(stop_playback):
                    stop_playback()
                else:
                    self._player.set_pause(True)
            except Exception:
                logger.warning(
                    "Failed to stop staged MPV target during transition abort",
                    exc_info=True,
                )
            # After a staged failure the retained committed queue can no
            # longer be trusted as a complete MPV-native playlist, even
            # when the transport cleanup itself failed.  Normalize it to
            # app-owned navigation so the queue data stays usable.
            self._deps.queue().normalize_after_native_loss()

        self._deps.set_current_track_info(None)
        self._deps.set_footer_owner("local")
        self._deps.mark_player_state_authoritative(self._player.state if self._player else {})

    def _build_restore_request(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any] | None,
        previous_state: Mapping[str, Any],
        track: Mapping[str, Any],
    ) -> TransitionRequest | None:
        """Build the Coordinator restore request for the committed source.

        The committed native-queue request fields are the single canonical
        source for both the restore decision and the carried playlist: a
        committed native queue was already validated for homogeneity at
        commit time, so the canonical gate is equivalent here.  Returns None
        when the committed source is not physically restorable.
        """
        source = str(track.get("source") or "")
        target_url = str(track.get("url") or previous_state.get("current_file") or "")
        if source not in {"local", "radio"} or not target_url:
            return None
        native_fields = self._deps.queue().native_request_fields()
        native_committed = bool(native_fields)
        # The authoritative restore rate comes from the previously committed
        # snapshot, not from the failed request: preferred positive
        # active_rate (the actually committed hardware rate), then positive
        # force_rate, then the track-derived Coordinator rate.
        snapshot_active = int((snapshot or {}).get("active_rate") or 0)
        snapshot_force = int((snapshot or {}).get("force_rate") or 0)
        restore_target_rate = (
            snapshot_active if snapshot_active > 0 else (snapshot_force if snapshot_force > 0 else 0)
        )
        if restore_target_rate <= 0:
            derived = self._deps.coordinator_target_rate(source, track)
            restore_target_rate = int(derived) if isinstance(derived, int) and derived > 0 else 0
        if restore_target_rate <= 0:
            logger.warning(
                "Failed-transition source restore aborted: no authoritative committed "
                "sample rate for %s source url=%s",
                source,
                target_url,
            )
            return None
        # rate_change is not a blind copy of the failed request: it must
        # cover both a real rate/DSP switch performed by the failed Spotify
        # handoff and a live state that currently differs from the committed
        # restore rate.  Unknown or missing live rate state counts as a
        # possible rate change (conservative), so effects/helper are
        # validated/reinitialized; establish_target_rate stays idempotent
        # when the hardware already stands correctly.
        try:
            live_status = dict(self._deps.get_samplerate_status())
        except Exception:
            live_status = {}
        live_active = int(live_status.get("active_rate") or 0)
        live_aligned = bool(live_active > 0 and live_active == restore_target_rate)
        restore_rate_change = bool(request.rate_change or not live_aligned)
        was_playing = bool(
            previous_state.get("playing")
            and not previous_state.get("paused")
            and not previous_state.get("ended")
        )
        previous_position = previous_state.get("position")
        restore_position = (
            max(0.0, float(previous_position))
            if source == "local"
            and isinstance(previous_position, (int, float))
            and previous_position > 0
            else None
        )
        return TransitionRequest(
            operation="replay",
            source=source,
            target_rate=restore_target_rate,
            target_url=target_url,
            target_track=dict(track),
            should_play=was_playing,
            rate_change=restore_rate_change,
            reload_source=True,
            restore_position=restore_position,
            native_queue=(
                tuple(native_fields["native_queue"]) if native_committed else None
            ),
            native_queue_index=native_fields.get("native_queue_index") if native_committed else None,
            native_queue_loop=bool(native_fields.get("native_queue_loop")) if native_committed else False,
            detail="failed-transition-restore",
        )

    async def publish_restored_source(self, request: TransitionRequest) -> None:
        """Publish a Coordinator-confirmed restored source as active context."""
        track = dict(request.target_track or {})
        if not track:
            return
        self._deps.set_current_track_info(track)
        self._deps.set_footer_owner("local")
        self._deps.mark_player_state_authoritative(self._player.state if self._player else {})

    async def normalize_queue_after_native_loss(self) -> None:
        """Normalize the retained queue to app-owned navigation after a
        failed native-playlist restore."""
        self._deps.queue().normalize_after_native_loss()

    async def validate_measurement_restore_intent(
        self,
        request: TransitionRequest,
        _snapshot: Mapping[str, Any] | None = None,
    ) -> bool:
        """Reject a measurement restore after the user changed playback intent."""
        intent = request.restore_intent or {}
        if not intent:
            return True

        expected_source = str(intent.get("source") or request.source)
        expected_spotify_identities: set[str] = set()
        if expected_source == "spotify":
            expected_spotify_identities = self._deps.spotify_snapshot_identity_values(intent)
            if not expected_spotify_identities:
                expected_spotify_identities = self._deps.spotify_snapshot_identity_values({
                    "target_url": request.target_url,
                    "track_info": request.target_track,
                })

        expected_id = intent.get("id")
        if expected_id in {None, ""}:
            expected_id = None
        return await self._deps.measurement_restore_intent_matches_live_state(
            expected_source=expected_source,
            expected_id=expected_id,
            expected_url=intent.get("url") or intent.get("path") or request.target_url,
            expected_file=intent.get("current_file"),
            expected_spotify_identities=expected_spotify_identities,
            intent_generation=intent.get("intent_generation"),
        )

