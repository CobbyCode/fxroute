# SPDX-License-Identifier: AGPL-3.0-only
"""Hardware channel-tier stages inside the existing playback transaction."""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from dataclasses import replace

import audio.samplerate as samplerate
import playback.source_policy as source_policy

logger = logging.getLogger(__name__)


class _RuntimeChannelTierMixin:
    async def _change_tier_hardware(self, change, *, rollback=False):
        lock_factory = self._deps.audio_configuration_lock
        async with lock_factory() if lock_factory else nullcontext():
            if not rollback and self._deps.measurement_audio_graph_owned():
                raise RuntimeError("Measurement is active; channel-tier switch is locked")
            if self._dsp_runtime is not None:
                await self._dsp_runtime.stop()
            try:
                await self._deps.drain_worker(change.rollback if rollback else change.apply)
            finally:
                self.invalidate_gate_sink_resolution()
        return await asyncio.to_thread(self._deps.get_audio_output_overview)

    async def apply_channel_tier(self, request, snapshot):
        stored = (snapshot or {}).get("channel_tier_change")
        wanted_tier = dict((request.channel_tier or {}).get("tier") or {})
        wanted_rate = getattr(request, "target_rate", None)
        if (
            stored is not None
            and stored.tier == wanted_tier
            and stored.target_rate == wanted_rate
        ):
            return await self._change_tier_hardware(stored)
        from audio.channel_tiers import prepare_change
        tiers = ((request.audio_overview or {}).get("selected_output") or {}).get(
            "device_profile", {}).get("tiers") or []
        if not wanted_tier or wanted_tier not in tiers:
            raise ValueError("Channel-tier transition has no known destination tier")
        change = prepare_change(
            str((request.channel_tier or {}).get("key") or ""),
            wanted_tier, wanted_rate, tiers,
        )
        if stored is None:
            # Coordinator-injected switches arrive without a pre-built change
            # (the target rate only resolves mid-transition): capture now,
            # still under the closed gate, and keep it for a later rollback.
            await self._deps.drain_worker(change.capture)
            try:
                snapshot["channel_tier_change"] = change
            except TypeError:
                # An immutable snapshot cannot carry the change: rollback later
                # finds nothing and reports False. Never seen with the current
                # dict snapshot; log so a lost rollback change is visible.
                logger.warning(
                    "Channel-tier change could not be stored on the transition snapshot; rollback will be unavailable"
                )
        else:
            # Later pass with a newly derived tier/rate: apply exactly this
            # target with a fresh change, but carry the original hardware
            # baseline instead of re-capturing. The current sink already
            # reflects an earlier pass, so a re-capture would move the
            # rollback point; the stored pass-1 change keeps restoring the
            # pre-transition state. The source selection already points at
            # the pro input, so there is nothing left to rewrite for it.
            change.volume = stored.volume
            change.old_source_selection = None
            change.captured = True
            change.volume_monitor_recovery_owner = stored
        return await self._change_tier_hardware(change)

    async def _previous_source_request(self, old_graph, snapshot):
        """Describe the pre-transition source for the rollback.

        Built from the snapshot only: the failed target is never a fallback.
        """
        graph_only = replace(
            old_graph, target_url=None, target_track=None, reload_source=False,
            should_play=False, restore_position=None,
            native_queue=None, native_queue_index=None, native_queue_loop=False,
        )
        if source_policy.is_external_source(old_graph.source):
            # An external target (Spotify/Qobuz) was paused by the failure
            # cleanup: it is neither restarted nor awaited here. The rollback
            # restores only the old tier graph; the committed MPV source
            # follows the regular failed-handoff restore in the cleanup.
            return graph_only
        old_player = dict(snapshot.get("player") or {})
        old_track = dict(snapshot.get("current_track") or {})
        if source_policy.is_mpv_source(old_track.get("source")) and old_player.get("current_file"):
            # Same source, URL, play/pause state, position and committed
            # queue as the failed-handoff restore of the committed source.
            restore = await self._build_restore_request(old_graph, snapshot, old_player, old_track)
            if restore is not None:
                return replace(
                    old_graph, source=restore.source, target_url=restore.target_url,
                    target_track=restore.target_track, reload_source=True,
                    should_play=restore.should_play,
                    restore_position=restore.restore_position,
                    native_queue=restore.native_queue,
                    native_queue_index=restore.native_queue_index,
                    native_queue_loop=restore.native_queue_loop,
                )
        # No previous MPV source: the failed target may be staged in MPV and
        # must not survive the rollback.
        await self._stop_staged_mpv_target()
        quieted = self._quieted_external_source
        if quieted:
            # The handoff paused a playing Spotify/qbzd renderer; resume it.
            # The renderer keeps its own track position while paused.
            return replace(graph_only, source=quieted, should_play=True)
        return graph_only

    async def _stop_staged_mpv_target(self) -> None:
        if not self._deps.player_is_running():
            return
        await self._deps.drain_worker(self._player.stop_playback)
        self._staged_target_url = None
        self._deps.mark_player_state_authoritative(self._player.state)

    async def rollback_channel_tier(self, request, snapshot, transition_id):
        snapshot = snapshot or {}
        change = snapshot.get("channel_tier_change")
        if change is None:
            return False
        if not change.started:
            return True
        overview = await self._change_tier_hardware(change, rollback=True)
        policy = snapshot.get("sample_rate_policy") or {"mode": "auto", "rate": None}
        samplerate.persist_sample_rate_policy(policy)
        old_player = snapshot.get("player") or {}
        old_graph = replace(
            request, channel_tier={}, target_rate=change.old_rate,
            audio_overview=overview, output_mode_target=overview,
            sample_rate_policy=policy, rate_change=True,
        )
        old_request = await self._previous_source_request(old_graph, snapshot)
        await self.establish_target_rate(old_request)
        await self.establish_effects_and_helper(old_request)
        if old_request.target_url or (
            old_request.should_play and source_policy.is_external_source(old_request.source)
        ):
            await self.prepare_target_source(old_request)
            await self.start_target_source(old_request)
        await self.reconcile_post_start_graph(old_request)
        await self.verify_output_mode_runtime(old_request)
        await self.set_source_volume(int(old_player.get("volume", 100)), transition_id)
        await self.stabilize_effects_after_rate_change(old_request, dsp_reinitialized=True)
        await self.verify_output_mode_runtime(old_request)
        return True
