# SPDX-License-Identifier: AGPL-3.0-only

"""Output-mode lifecycle operations (commit, rollback, finalize) of the adapter.

Extracted from :class:`FxrouteTransitionRuntime`; runs on the composing
adapter instance and reads the attributes declared on the class below.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Mapping

import audio.samplerate as samplerate
from streaming.spotify.provider import play as spotify_play
import playback.source_policy as source_policy
from playback.transition import TransitionRequest, stable_graph_readbacks

from .deps import PlaybackRuntimeDependencies

logger = logging.getLogger(__name__)


class _RuntimeOutputModeMixin:
    """Attributes provided by the composing adapter instance."""
    _deps: PlaybackRuntimeDependencies
    _dsp_runtime: Any

    def _output_state_service(self):
        """Return the bound output service for v2 transitions."""
        getter = self._deps.get_output_service
        service = getter() if callable(getter) else None
        if service is None:
            raise RuntimeError("output-state transition has no output service")
        return service

    async def commit_output_mode_runtime(self, request: TransitionRequest) -> dict[str, Any]:
        """Commit the v2 candidate only after the guarded graph readback.

        The edit was checked against measurement ownership before the
        transition started; a job that took the graph since then refuses the
        commit and the transition rolls the graph back.
        """
        v2 = request.output_state_transition or {}
        if v2.get("candidate_state") is None:
            raise RuntimeError("output-mode transition has no v2 candidate state")
        service = self._output_state_service()
        expected_revision = v2.get("expected_revision")
        if type(expected_revision) is bool or not isinstance(expected_revision, int):
            raise RuntimeError("output-state transition has no base revision")
        committed = service.commit_unowned(v2["candidate_state"],
                                           expected_revision=expected_revision)
        return {
            "output_mode_persisted": True,
            "output_state_revision": committed["revision"],
            "output_fingerprint": v2.get("fingerprint"),
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
        """Revert a v2 candidate while the failure gate is closed."""
        v2 = request.output_state_transition or {}
        if v2.get("candidate_state") is None:
            raise RuntimeError("output-mode rollback has no v2 candidate state")
        await self._rollback_output_state(request, v2)

    async def _rollback_output_state(self, request: TransitionRequest, v2: Mapping[str, Any]) -> None:
        """Revert a v2 candidate and resync the previous graph, best-effort.

        The revert only lands when the failed candidate is still the head
        revision: a concurrent newer commit owns the store and the engine
        then, and clobbering either would trade one drift for another.
        """
        service = self._output_state_service()
        current = service.load()
        current_fingerprint = None
        if current.get("revision") is not None:
            try:
                current_fingerprint = service.fingerprint(
                    current, output_key=str(v2.get("output_key") or ""),
                    channels=int(v2.get("channels") or 0),
                    sample_rate_hz=int(request.target_rate or 0))
            except (FileNotFoundError, ValueError):
                current_fingerprint = None
        previous_target = v2.get("previous_target")
        runtime = self._dsp_runtime
        if current_fingerprint == v2.get("fingerprint"):
            reverted = service.revert(v2["previous_state"],
                                      expected_revision=int(current.get("revision")))
            logger.info("Output-state rollback recommitted revision %s", reverted.get("revision"))
        elif current == v2.get("previous_state"):
            # The persist stage never ran (or a repeated rollback already
            # reverted): the store needs no repair, but the engine may still
            # hold the staged candidate graph.
            logger.info("Output-state store already at previous revision; resyncing graph only")
        else:
            logger.warning(
                "Skipping output-state rollback: store head is not the failed candidate")
            return
        if previous_target is not None and runtime is not None:
            await runtime.sync_rendered(previous_target)
            await self._verify_plan_rollback(request, previous_target)
        else:
            logger.warning(
                "Output-state rollback has no previous graph target; "
                "the engine may hold an uncommitted candidate graph")

    async def _verify_plan_rollback(self, request: TransitionRequest, previous_target) -> None:
        """Confirm the resynced previous graph with two stable readbacks."""
        config = previous_target.config
        overview = {"output_mode": {
            "mode": config.output_mode,
            "effective_output_key": config.output_key,
            "planned_routes": [[signal, port] for signal, port in config.route_pairs]}}
        readbacks, _, stable = await stable_graph_readbacks(
            lambda: self._deps.playback_graph_diagnosis(
                overview, target_rate=request.target_rate, require_source=False))
        if not stable:
            raise RuntimeError("previous plan graph could not be restored")

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
