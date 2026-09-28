# SPDX-License-Identifier: AGPL-3.0-only
"""Hardware channel-tier stages inside the existing playback transaction."""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from dataclasses import replace

import audio.samplerate as samplerate

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
        return await self._change_tier_hardware(change)

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
        old_request = replace(
            request, channel_tier={}, target_rate=change.old_rate,
            audio_overview=overview, output_mode_target=overview,
            sample_rate_policy=policy, rate_change=True,
            target_url=old_player.get("current_file") or request.target_url,
            target_track=snapshot.get("current_track") or request.target_track,
            reload_source=bool(old_player.get("current_file") or request.target_url),
            restore_position=old_player.get("position"),
        )
        await self.establish_target_rate(old_request)
        await self.establish_effects_and_helper(old_request)
        if old_request.target_url:
            await self.prepare_target_source(old_request)
            await self.start_target_source(old_request)
        await self.reconcile_post_start_graph(old_request)
        await self.verify_output_mode_runtime(old_request)
        await self.set_source_volume(int(old_player.get("volume", 100)), transition_id)
        await self.stabilize_effects_after_rate_change(old_request, dsp_reinitialized=True)
        await self.verify_output_mode_runtime(old_request)
        return True
