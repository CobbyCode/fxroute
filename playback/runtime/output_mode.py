# SPDX-License-Identifier: AGPL-3.0-only

"""Output-mode lifecycle operations (commit, rollback, finalize) of the adapter.

Extracted from :class:`FxrouteTransitionRuntime`; runs on the composing
adapter instance and reads the attributes declared on the class below.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import replace
from typing import Any, Mapping

import audio.samplerate as samplerate
from audio.samplerate import (
    OUTPUT_MODE_STEREO,
    OUTPUT_MODE_SUBWOOFER_22_MODES,
    OUTPUT_MODE_SUBWOOFER_MODES,
)
from streaming.spotify.provider import play as spotify_play
import playback.source_policy as source_policy
from playback.transition import TransitionRequest, stable_graph_readbacks

from .deps import PlaybackRuntimeDependencies

logger = logging.getLogger(__name__)


class _RuntimeOutputModeMixin:
    """Attributes provided by the composing adapter instance."""
    _deps: PlaybackRuntimeDependencies

    async def commit_output_mode_runtime(self, request: TransitionRequest) -> dict[str, Any]:
        """Write the target mode only after the guarded graph readback."""
        if not request.output_mode_config:
            raise RuntimeError("output-mode transition has no durable target config")
        result = self._deps.persist_audio_output_mode(request.output_mode_config)
        return {
            "output_mode_persisted": True,
            "output_mode": dict(result.get("output_mode") or {}),
        }

    async def commit_sample_rate_policy(self, request: TransitionRequest) -> dict[str, Any]:
        if not request.sample_rate_policy:
            raise RuntimeError("sample-rate transition has no durable policy")
        policy = samplerate.persist_sample_rate_policy(request.sample_rate_policy)
        if policy.get("mode") == "auto":
            # The guarded readback already verified the graph is stable at the
            # target.  A leftover force-rate pin at the graph default (written
            # by the target-rate stage) would keep the samplerate status
            # payload reporting mode=fixed under an auto policy.  Clearing the
            # pin here, after commit, is safe: the graph holds the default
            # rate without it.  Mid-transition clears are deliberately avoided:
            # the DSP helper may still be rebuilding at the old rate and would
            # pull the sink away from the freshly cleared pin.
            samplerate.clear_auto_policy_force_rate(
                request.target_rate, app_policy=policy
            )
        return {"sample_rate_policy": policy}

    async def rollback_sample_rate_policy(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any] | None,
    ) -> None:
        """Restore the pre-transition policy and its live force-rate pin.

        The transition may have re-pinned or cleared the PipeWire force-rate
        before failing, so restoring only the JSON policy would leave the
        graph pinned to the failed target (auto) or unpinned (fixed).  The
        snapshot carries the authoritative pre-transition rate; the durable
        policy write comes first so a pin restore failure still leaves the
        configuration consistent with the policy.
        """
        previous_policy = (snapshot or {}).get("sample_rate_policy")
        if not isinstance(previous_policy, Mapping):
            raise RuntimeError("sample-rate rollback has no previous policy")
        samplerate.persist_sample_rate_policy(previous_policy)
        previous_rate = (snapshot or {}).get("active_rate")
        previous_force = (snapshot or {}).get("force_rate")
        if isinstance(previous_rate, int) and previous_rate > 0:
            if previous_policy.get("mode") == "fixed" and isinstance(previous_force, int) and previous_force > 0:
                try:
                    samplerate.set_pipewire_force_rate(previous_force)
                except Exception as exc:
                    logger.warning(
                        "Sample-rate rollback force-rate restore failed: rate=%s error=%s",
                        previous_force,
                        exc,
                    )
            elif previous_policy.get("mode") == "auto":
                samplerate.clear_auto_policy_force_rate(
                    previous_rate,
                    app_policy=previous_policy,
                    status={"active_rate": previous_rate},
                )

    async def rollback_output_mode_runtime(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any] | None,
    ) -> None:
        """Restore the old mode graph/config while the failure gate is closed."""
        snapshot = snapshot or {}
        old_overview = snapshot.get("output_mode_overview")
        old_config = snapshot.get("output_mode_config")
        if not isinstance(old_overview, Mapping):
            raise RuntimeError("output-mode rollback has no previous overview")
        old_overview = copy.deepcopy(dict(old_overview))
        old_mode = (old_overview.get("output_mode") or {}).get("mode")
        old_preset = snapshot.get("dsp_active_preset")
        if not isinstance(old_config, Mapping) or not old_config:
            old_output_mode = dict(old_overview.get("output_mode") or {})
            old_config = {"mode": old_mode or OUTPUT_MODE_STEREO}
            if old_mode in OUTPUT_MODE_SUBWOOFER_22_MODES:
                if old_output_mode.get("subwoofers"):
                    old_config["subwoofers"] = copy.deepcopy(old_output_mode["subwoofers"])
                if old_output_mode.get("subwoofer"):
                    old_config["subwoofer"] = copy.deepcopy(old_output_mode["subwoofer"])
            else:
                old_config["subwoofer"] = copy.deepcopy(old_output_mode.get("subwoofer") or {})
        # Restore persistence first.  If the old graph cannot be rebuilt, the
        # durable mode still cannot claim the failed target configuration.
        self._deps.persist_audio_output_mode(old_config)
        if self._dsp_manager is not None and old_preset:
            current_preset = self._dsp_manager.get_active_preset()
            if current_preset != old_preset:
                await self._deps.load_dsp_preset(old_preset, convolver_sample_rate_hz=request.target_rate)
        await self._deps.sync_dsp_runtime(
            old_overview,
            reason="coordinator-output-mode-rollback",
            target_overview=old_overview,
        )
        if old_mode in OUTPUT_MODE_SUBWOOFER_MODES:
            await self._deps.coordinator_reconcile_subwoofer_links_only()
        rollback_request = replace(request, output_mode_target=old_overview)
        await self._verify_output_mode_rollback(rollback_request, old_mode)

    async def _verify_output_mode_rollback(
        self,
        request: TransitionRequest,
        _old_mode: Any,
    ) -> None:
        readbacks, _, stable = await stable_graph_readbacks(
            lambda: self._deps.playback_graph_diagnosis(
                request.output_mode_target,
                target_rate=request.target_rate,
                require_source=False,
            )
        )
        if not stable:
            raise RuntimeError("previous output-mode graph could not be restored")

    async def restore_output_mode_transport(
        self,
        request: TransitionRequest,
        snapshot: Mapping[str, Any] | None,
        transition_id: str,
    ) -> None:
        """Restore playing/paused transport after the mode graph commits."""
        snapshot = snapshot or {}
        previous_player = dict(snapshot.get("player") or {})
        previous_spotify = dict(snapshot.get("spotify") or {})
        if source_policy.is_mpv_source(request.source) and self._deps.player_is_running():
            previous_volume = previous_player.get("volume")
            if isinstance(previous_volume, (int, float)):
                await self.set_source_volume(int(round(previous_volume)), transition_id)
            should_play = bool(
                previous_player.get("playing")
                and not previous_player.get("paused")
                and not previous_player.get("ended")
            )
            await self._deps.drain_worker(
                self._player.set_pause, not should_play
            )
            state = dict(self._player.state if self._player else {})
            if should_play and (state.get("paused") or not state.get("playing")):
                raise RuntimeError("local transport did not resume after output-mode commit")
            if not should_play and not state.get("paused"):
                raise RuntimeError("local pause state did not survive output-mode commit")
        elif request.source == "spotify":
            should_play = previous_spotify.get("status") == "Playing"
            if should_play:
                data = await spotify_play()
                if data.get("status") not in {"Playing", "playing"}:
                    raise RuntimeError("Spotify did not resume after output-mode commit")
            else:
                await self._deps.spotify_pause()
        elif request.source == "qobuz":
            should_play = (snapshot.get("qobuz") or {}).get("status") == "Playing"
            if should_play:
                await self.start_target_source(replace(request, should_play=True))
            else:
                await self._deps.qobuz_pause()

    async def reconcile_post_start_graph(self, request: TransitionRequest) -> dict[str, Any]:
        """Run the bounded final graph reconciliation before staged commit."""
        return await self._deps.coordinator_reconcile_post_start_graph(request)

    async def finalize_output_mode_graph_after_gate_open(
        self, request: TransitionRequest
    ) -> dict[str, Any]:
        """Remove links recreated when the newly opened hardware sink activates."""
        overview = request.output_mode_target
        if not isinstance(overview, Mapping):
            raise RuntimeError("output-mode transition has no target overview")
        mode = (overview.get("output_mode") or {}).get("mode")
        if mode in OUTPUT_MODE_SUBWOOFER_MODES:
            await self._deps.coordinator_reconcile_subwoofer_links_only()

        # An output-mode switch never (re)starts its source (see the
        # post-start reconcile): only a playing source has stream links.
        source_required = bool(request.should_play)
        diagnosis = await self._deps.playback_graph_diagnosis(
            overview,
            source=request.source if source_required else None,
            target_rate=request.target_rate,
            require_source=source_required,
        )
        if not diagnosis.get("links_complete"):
            raise RuntimeError(
                "post-gate output-mode graph is incomplete: "
                f"{diagnosis.get('signature')}"
            )
        return {"graph_complete": True, "diagnosis": diagnosis}
