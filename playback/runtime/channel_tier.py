# SPDX-License-Identifier: AGPL-3.0-only
"""Hardware channel-tier stages inside the existing playback transaction."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace

import audio.samplerate as samplerate


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
        return await self._change_tier_hardware(snapshot["channel_tier_change"])

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
