# SPDX-License-Identifier: AGPL-3.0-only

"""Commit and readback verification operations of the runtime adapter.

Extracted from :class:`FxrouteTransitionRuntime`; runs on the composing
adapter instance and reads the attributes declared on the class below.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any, Mapping

import audio.samplerate as samplerate
from audio.samplerate import OUTPUT_MODE_STEREO, OUTPUT_MODE_SUBWOOFER_MODES
import playback.source_policy as source_policy
from playback.transition import TransitionRequest, stable_graph_readbacks

from .deps import PlaybackRuntimeDependencies

logger = logging.getLogger(__name__)


class _RuntimeVerificationMixin:
    """Attributes provided by the composing adapter instance."""
    _deps: PlaybackRuntimeDependencies

    async def _read_and_validate_effects_runtime(
        self, extras: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Read and validate the confirmed DSP work point for the given extras.

        The loudness/autogain work point is a deterministic derivation of the
        confirmed extras; the native engine is the authority on liveness.  The
        runtime readback therefore requires a running engine and reports the
        derived work point, which is validated against the LSP control range
        and the enabled/bypass invariants.
        """
        manager = self._dsp_manager
        runtime = self._deps.dsp_runtime()
        if manager is None:
            return {}

        loudness = extras.get("loudness") or {}
        autogain = extras.get("autogain") or {}
        loudness_enabled = bool(loudness.get("enabled"))
        autogain_enabled = bool(autogain.get("enabled"))
        result: dict[str, Any] = {}

        read_loudness = getattr(runtime, "read_loudness_runtime", None)
        if callable(read_loudness):
            try:
                loudness_runtime = await asyncio.to_thread(read_loudness, extras)
            except Exception:
                if loudness_enabled:
                    raise
                loudness_runtime = None
            if isinstance(loudness_runtime, dict):
                try:
                    actual_volume = float(loudness_runtime["volume"])
                    actual_output_gain = float(loudness_runtime["output_gain"])
                except (KeyError, TypeError, ValueError) as exc:
                    if loudness_enabled:
                        raise RuntimeError(
                            f"DSP Loudness readback is incomplete: {exc}"
                        ) from exc
                    loudness_runtime = None
                if isinstance(loudness_runtime, dict):
                    minimum = float(manager.LOUDNESS_PLUGIN_VOLUME_MIN_DB)
                    maximum = float(manager.LOUDNESS_PLUGIN_VOLUME_MAX_DB)
                    if not minimum <= actual_volume <= maximum:
                        raise RuntimeError(
                            "DSP Loudness volume is outside the installed LSP range: "
                            f"{actual_volume} not in [{minimum}, {maximum}]"
                        )
                    if loudness_enabled and loudness_runtime.get("bypass"):
                        raise RuntimeError(
                            "DSP Loudness was bypassed after DSP stabilization"
                        )
                    result["loudness"] = {
                        "volume": actual_volume,
                        "output_gain": actual_output_gain,
                        "bypass": bool(loudness_runtime.get("bypass")),
                    }
        elif loudness_enabled:
            raise RuntimeError("DSP Loudness readback is unavailable")

        read_autogain = getattr(runtime, "read_autogain_runtime", None)
        if callable(read_autogain):
            try:
                autogain_runtime = await asyncio.to_thread(read_autogain, extras)
            except Exception:
                if autogain_enabled:
                    raise
                autogain_runtime = None
            if isinstance(autogain_runtime, dict):
                try:
                    actual_target = float(autogain_runtime["target"])
                except (KeyError, TypeError, ValueError) as exc:
                    if autogain_enabled:
                        raise RuntimeError(
                            f"DSP Auto Gain readback is incomplete: {exc}"
                        ) from exc
                    autogain_runtime = None
                if isinstance(autogain_runtime, dict):
                    if autogain_enabled and autogain_runtime.get("bypass"):
                        raise RuntimeError(
                            "DSP Auto Gain was bypassed after DSP stabilization"
                        )
                    result["autogain"] = {
                        "target": actual_target,
                        "bypass": bool(autogain_runtime.get("bypass")),
                    }
        elif autogain_enabled:
            raise RuntimeError("DSP Auto Gain readback is unavailable")

        return result

    async def stabilize_effects_after_rate_change(
        self,
        request: TransitionRequest,
        *,
        dsp_reinitialized: bool = False,
    ) -> dict[str, Any]:
        """Re-apply the canonical DSP work point after a rate/DSP mutation.

        An output-mode switch may reload the compare preset or rebuild the
        DSP graph, either of which can re-apply a stale preset
        loudness work point over the user volume.
        The switch therefore re-applies the canonical extras unconditionally
        instead of gating on rate_change/dsp_reinitialized, so the volume
        never resurrects an older preset value.
        """
        if not (
            request.rate_change
            or dsp_reinitialized
            or request.operation == "output-mode-switch"
        ):
            return {"stabilized": True, "no_op": True}
        if (
            not request.should_play
            and request.operation not in {"measurement-entry", "output-mode-switch"}
        ):
            return {"stabilized": True, "no_op": True}

        manager = self._dsp_manager
        if manager is None:
            return {"stabilized": True, "no_op": True}

        # The whole guarded re-apply runs under the central DSP
        # mutation ownership, and the canonical extras are (re)read after
        # acquiring it: a parallel volume/extras/SPL mutation must never be
        # clobbered by a stale pre-lock snapshot.  The ownership is held
        # through the settle and the runtime readback/validation so the
        # coordinator validates the live DSP against the very extras it
        # applied, never against a snapshot made stale by a mutation that
        # landed between apply and verify.
        async with self._deps.dsp_mutation_lock():
            extras = manager.load_global_extras()
            loudness_enabled = bool((extras.get("loudness") or {}).get("enabled"))
            autogain_enabled = bool((extras.get("autogain") or {}).get("enabled"))
            if not loudness_enabled and not autogain_enabled:
                return {"stabilized": True, "no_op": True}

            apply_runtime = getattr(manager, "apply_autogain_loudness_runtime", None)
            if not callable(apply_runtime):
                raise RuntimeError("guarded Auto Gain/Loudness runtime is unavailable")

            # Passing the same canonical extras on both sides deliberately uses the
            # existing guarded order without reloading the preset.  The helper
            # method keeps the outputGain guard in place while the LSP volume port
            # settles, then ramps back to the canonical work point.  The blocking
            # manager call runs off the event loop; the caller owns the mutation
            # lock until the worker actually finished (cancellation-safe).
            await self._deps.drain_worker(
                apply_runtime,
                extras,
                extras,
                persist_all_presets=False,
            )
            settle_seconds = float(
                getattr(manager, "LOUDNESS_STRENGTH_VOLUME_SETTLE_SECONDS", 0.0)
            )
            if settle_seconds > 0:
                await asyncio.sleep(settle_seconds)

            effects_runtime = await self._read_and_validate_effects_runtime(extras)

        rate = dict(self._deps.get_samplerate_status())
        if rate.get("active_rate") != request.target_rate:
            raise RuntimeError(
                "target rate changed during DSP stabilization: "
                f"expected={request.target_rate} actual={rate.get('active_rate')}"
            )
        if rate.get("force_rate") not in {None, 0, request.target_rate}:
            raise RuntimeError(
                f"force-rate changed during DSP stabilization: {rate.get('force_rate')}"
            )
        graph_overview = (
            request.output_mode_target
            if request.operation == "output-mode-switch" and request.output_mode_target
            else None
        )
        source_required = bool(
            request.should_play
            or (request.operation == "output-mode-switch" and bool(request.target_url))
        )
        graph = await self._deps.playback_graph_diagnosis(
            graph_overview,
            source=request.source if source_required else None,
            target_rate=request.target_rate,
            require_source=source_required,
        )
        if not graph.get("links_complete"):
            # Link-only drift during DSP stabilization (EE reconfigures its
            # graph while the guarded runtime apply settles): one bounded
            # repair attempt, then re-read before failing the transition.
            graph_mode = graph.get("mode")
            try:
                if graph_mode == OUTPUT_MODE_STEREO:
                    await self._deps.repair_stereo_output_links_once(graph)
                elif graph_mode in OUTPUT_MODE_SUBWOOFER_MODES:
                    await self._deps.coordinator_reconcile_subwoofer_links_only()
                else:
                    graph_mode = None
                if graph_mode is not None:
                    graph = await self._deps.playback_graph_diagnosis(
                        graph_overview,
                        source=request.source if source_required else None,
                        target_rate=request.target_rate,
                        require_source=source_required,
                    )
            except Exception as exc:
                logger.warning(
                    "DSP stabilization link repair failed: %s",
                    exc,
                )
        if not graph.get("links_complete"):
            raise RuntimeError(
                "production graph changed during DSP stabilization: "
                f"{graph.get('signature')}"
            )

        result = {
            "stabilized": True,
            "no_op": False,
            "active_rate": rate.get("active_rate"),
            "force_rate": rate.get("force_rate"),
            "graph_complete": True,
            "graph_signature": graph.get("signature"),
            "effects_runtime": effects_runtime,
        }
        logger.info(
            "Playback transition post-start DSP stabilization complete: "
            "source=%s rate=%s force=%s loudness=%s autogain=%s graph=%s",
            request.source,
            result["active_rate"],
            result["force_rate"],
            effects_runtime.get("loudness"),
            effects_runtime.get("autogain"),
            result["graph_signature"],
        )
        return result

    def _live_mpv_commit_state(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Read live MPV IPC props (pause, idle-active, volume) for commit validation.

        The cached state is driven by the async mpv event listener and can lag
        a pause/unload/volume change mpv already applied.  Read live IPC at the
        commit boundary so a source that was re-paused or unmuted after start
        cannot commit silently.  Small test adapters without ``get_property``
        keep the cached-state fallback path.
        """
        get_property = getattr(self._player, "get_property", None)
        live_mpv: dict[str, Any] = {}
        if callable(get_property):
            for prop in ("pause", "idle-active", "volume"):
                try:
                    live_mpv[prop] = get_property(prop)
                except Exception:
                    pass
        return live_mpv

    def _verify_mpv_live_commit(
        self,
        request: TransitionRequest,
        state: Mapping[str, Any],
        live_mpv: Mapping[str, Any],
        *,
        require_playing: bool,
        require_source_volume: bool,
        stage_label: str,
    ) -> int | None:
        """Confirm MPV live state at a commit boundary.

        Shared by the standard commit verifier and the same-graph fast path:
        checks the current_file identity, the live pause/idle state (falling
        back to the cached state for small test adapters) and the restored
        source volume.  Returns the effective live volume, or None when
        unavailable.
        """
        if request.target_url and state.get("current_file") != request.target_url:
            raise RuntimeError(
                f"MPV current_file mismatch: expected={request.target_url} actual={state.get('current_file')}"
            )
        live_paused = live_mpv.get("pause")
        live_idle = live_mpv.get("idle-active")
        if isinstance(live_paused, bool) and isinstance(live_idle, bool):
            if require_playing and (live_paused is not False or live_idle is not False):
                raise RuntimeError(f"MPV is not actually playing at {stage_label} (live IPC)")
            if not require_playing and live_paused is not True:
                raise RuntimeError(f"MPV pause state was not confirmed at {stage_label} (live IPC)")
        else:
            if require_playing and (state.get("paused") or not state.get("playing")):
                raise RuntimeError(f"MPV is not actually playing at {stage_label}")
            if not require_playing and not state.get("paused"):
                raise RuntimeError(f"MPV pause state was not confirmed at {stage_label}")
        live_volume = live_mpv.get("volume")
        if isinstance(live_volume, (int, float)):
            live_volume = int(round(float(live_volume)))
        else:
            live_volume = state.get("volume")
        if require_source_volume:
            if request.operation == "measurement-restore" and source_policy.is_mpv_source(request.source):
                if live_volume != 100:
                    raise RuntimeError(f"MPV source volume was not restored: {live_volume}")
            elif request.should_play and live_volume is not None and live_volume != 100:
                raise RuntimeError(f"MPV source volume was not restored: {live_volume}")
        return live_volume

    async def _verify_transition(
        self,
        request: TransitionRequest,
        *,
        require_source_volume: bool,
        require_effects_runtime: bool = True,
    ) -> dict[str, Any]:
        try:
            rate = dict(self._deps.get_samplerate_status())
        except Exception:
            rate = {}
        state = dict(self._player.state if self._player else {})
        overview = (
            dict(request.audio_overview)
            if request.audio_overview
            else self._deps.get_audio_output_overview()
        )
        live_mpv = self._live_mpv_commit_state(state)
        if source_policy.is_mpv_source(request.source):
            self._verify_mpv_live_commit(
                request,
                state,
                live_mpv,
                require_playing=request.should_play,
                require_source_volume=require_source_volume,
                stage_label="transition commit",
            )
        elif request.source == "spotify":
            spotify_state = await self._deps.get_spotify_ui_state()
            expected_status = "Playing" if request.should_play else "Paused"
            if request.should_play and spotify_state.get("status") != expected_status:
                raise RuntimeError(f"Spotify status was not confirmed: {spotify_state.get('status')}")
            if not request.should_play and spotify_state.get("status") == "Playing":
                raise RuntimeError("Spotify pause state was not confirmed at transition commit")
        elif request.source == "qobuz":
            qobuz_state = await self._deps.get_qobuz_ui_state()
            expected_status = "Playing" if request.should_play else "Paused"
            if request.should_play and qobuz_state.get("status") != expected_status:
                raise RuntimeError(f"Qobuz status was not confirmed: {qobuz_state.get('status')}")
            if not request.should_play and qobuz_state.get("status") == "Playing":
                raise RuntimeError("Qobuz pause state was not confirmed at transition commit")

        spotify_stream_rate = None
        qobuz_stream_rate = None
        if request.source == "spotify" and request.should_play:
            source_rate = self._deps.coordinator_source_rate("spotify", request.target_track)
            spotify_stream_rate = await self._deps.wait_for_spotify_sink_input_samplerate(expected_rate=source_rate)
            if (
                isinstance(source_rate, int)
                and spotify_stream_rate != source_rate
            ):
                raise RuntimeError(
                    "Spotify stream rate mismatch at commit: "
                    f"expected={source_rate} actual={spotify_stream_rate}"
                )
        if request.source == "qobuz" and request.should_play:
            source_rate = self._deps.coordinator_source_rate("qobuz", request.target_track)
            qobuz_stream_rate = await self._deps.wait_for_qobuz_sink_input_samplerate(expected_rate=source_rate)
            if (
                isinstance(source_rate, int)
                and qobuz_stream_rate != source_rate
            ):
                raise RuntimeError(
                    "Qobuz stream rate mismatch at commit: "
                    f"expected={source_rate} actual={qobuz_stream_rate}"
                )

        if isinstance(request.target_rate, int) and request.target_rate > 0:
            if rate.get("active_rate") != request.target_rate:
                raise RuntimeError(
                    f"hardware rate mismatch at commit: expected={request.target_rate} actual={rate.get('active_rate')}"
                )
            if rate.get("force_rate") not in {None, 0, request.target_rate}:
                raise RuntimeError(f"force-rate mismatch at commit: {rate.get('force_rate')}")
        graph_complete = await self._deps.playback_graph_links_complete(
            audio_overview=overview,
            source=request.source,
            target_rate=request.target_rate,
            require_source=True,
        )
        if not graph_complete:
            raise RuntimeError("production playback links were not complete at commit")

        helper_rate = None
        try:
            output_mode = (overview.get("output_mode") or {}).get("mode")
            if output_mode in OUTPUT_MODE_SUBWOOFER_MODES:
                if self._dsp_runtime is None:
                    raise RuntimeError("subwoofer helper runtime is not available at commit")
                helper_snapshot = self._dsp_runtime.snapshot()
                helper_rate = self._deps.helper_argument_sample_rate(helper_snapshot)
                if not helper_snapshot.get("active") or helper_rate != request.target_rate:
                    raise RuntimeError(
                        "subwoofer helper rate/state mismatch at commit: "
                        f"expected={request.target_rate} actual={helper_rate} "
                        f"active={helper_snapshot.get('active')}"
                    )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"subwoofer helper readback failed at commit: {exc}") from exc

        effects_runtime = {}
        if self._dsp_manager and require_effects_runtime:
            preset = await asyncio.to_thread(self._dsp_manager.get_active_preset)
            if not preset:
                raise RuntimeError("DSP active preset was not confirmed at commit")
            extras = self._dsp_manager.load_global_extras()
            try:
                effects_runtime = await self._read_and_validate_effects_runtime(extras)
            except RuntimeError:
                # Recoverable DSP work-point drift (e.g. a stale SPL-noise
                # state surviving a DSP restart/preset reload):
                # re-apply the canonical runtime once under the still-closed
                # gate and re-validate before failing the transition.
                apply_runtime = getattr(
                    self._dsp_manager, "apply_autogain_loudness_runtime", None
                )
                if not callable(apply_runtime):
                    raise
                logger.warning(
                    "Playback commit effects runtime drifted; re-applying canonical "
                    "runtime: operation=%s",
                    request.operation,
                )
                # Re-acquire the mutation ownership and re-read the canonical
                # extras under it before re-applying: the runtime drift may be
                # observed while a volume/extras/SPL mutation is in flight.
                # The ownership stays held through the re-validation so the
                # re-applied runtime is verified against the very extras that
                # were re-read, not a snapshot a parallel mutation made stale.
                async with self._deps.dsp_mutation_lock():
                    extras = self._dsp_manager.load_global_extras()
                    await self._deps.drain_worker(
                        apply_runtime, extras, extras, persist_all_presets=False
                    )
                    effects_runtime = await self._read_and_validate_effects_runtime(
                        extras
                    )
        return {
            "committed": True,
            "player": state,
            "active_rate": rate.get("active_rate"),
            "force_rate": rate.get("force_rate"),
            "spotify_stream_rate": spotify_stream_rate,
            "graph_complete": True,
            "helper_rate": helper_rate,
            "source_volume": state.get("volume"),
            "effects_runtime": effects_runtime,
        }

    async def verify_transition_graph(self, request: TransitionRequest) -> dict[str, Any]:
        """Verify the production graph while the output gate is still closed."""
        return await self._verify_transition(
            request,
            require_source_volume=False,
            require_effects_runtime=False,
        )

    async def verify_measurement_entry(self, request: TransitionRequest) -> dict[str, Any]:
        """Confirm the paused measurement handoff without starting music."""
        status = dict(self._deps.get_samplerate_status())
        if not samplerate.playback_rate_aligned(status, request.target_rate):
            await self._deps.reconcile_transition_sink_rate(
                request.target_rate, reason="measurement-entry"
            )
            status = dict(self._deps.get_samplerate_status())
        if status.get("active_rate") != request.target_rate:
            raise RuntimeError(
                "measurement entry hardware rate mismatch: "
                f"expected={request.target_rate} actual={status.get('active_rate')}"
            )
        if status.get("force_rate") not in {None, 0, request.target_rate}:
            raise RuntimeError(
                "measurement entry force-rate mismatch: "
                f"expected={request.target_rate} actual={status.get('force_rate')}"
            )

        readbacks, signatures, stable = await stable_graph_readbacks(
            lambda: self._deps.playback_graph_diagnosis(
                target_rate=request.target_rate,
                require_source=False,
            )
        )
        if not stable:
            final = readbacks[-1] if readbacks else {}
            self._deps.log_playback_graph_diagnosis(
                final,
                target_rate=int(request.target_rate or 0),
                reason="measurement-entry",
                detail=request.detail,
            )
            raise RuntimeError(
                "measurement entry canonical graph did not reach two stable readbacks"
            )

        if source_policy.is_mpv_source(request.source) and request.target_url:
            state = dict(self._player.state if self._player else {})
            if state.get("current_file") != request.target_url:
                raise RuntimeError(
                    "measurement entry changed the loaded music source: "
                    f"expected={request.target_url} actual={state.get('current_file')}"
                )
            if not state.get("paused"):
                raise RuntimeError("music source was not left paused for measurement")
        elif request.source == "spotify":
            spotify_state = await self._deps.get_spotify_ui_state()
            if spotify_state.get("status") == "Playing":
                raise RuntimeError("Spotify was not left paused for measurement")
        elif request.source == "qobuz":
            qobuz_state = await self._deps.get_qobuz_ui_state()
            if qobuz_state.get("status") == "Playing":
                raise RuntimeError("Qobuz was not left paused for measurement")

        return {
            "committed": True,
            "measurement_entry": True,
            "active_rate": status.get("active_rate"),
            "force_rate": status.get("force_rate"),
            "graph_complete": True,
            "graph_signature": signatures[-1],
        }

    async def verify_output_mode_runtime(self, request: TransitionRequest) -> dict[str, Any]:
        """Confirm a target output-mode graph before its durable config write."""
        target_overview = copy.deepcopy(request.output_mode_target)
        if not target_overview:
            raise RuntimeError("output-mode transition has no target overview")
        target_rate = request.target_rate
        if not isinstance(target_rate, int) or target_rate <= 0:
            raise RuntimeError("output-mode transition has no authoritative sample rate")

        rate = dict(self._deps.get_samplerate_status())
        if not samplerate.playback_rate_aligned(rate, target_rate):
            await self._deps.reconcile_transition_sink_rate(
                target_rate, reason="output-mode-switch"
            )
            rate = dict(self._deps.get_samplerate_status())
        if rate.get("active_rate") != target_rate:
            raise RuntimeError(
                "output-mode transition hardware rate mismatch: "
                f"expected={target_rate} actual={rate.get('active_rate')}"
            )
        if rate.get("force_rate") not in {None, 0, target_rate}:
            raise RuntimeError(
                "output-mode transition force-rate mismatch: "
                f"expected={target_rate} actual={rate.get('force_rate')}"
            )

        readbacks, signatures, stable = await stable_graph_readbacks(
            lambda: self._deps.playback_graph_diagnosis(
                target_overview,
                source=(
                    request.source
                    if request.target_url or request.should_play
                    else None
                ),
                target_rate=target_rate,
                require_source=bool(request.target_url or request.should_play),
            )
        )
        if not stable:
            final = readbacks[-1] if readbacks else {}
            self._deps.log_playback_graph_diagnosis(
                final,
                target_rate=target_rate,
                reason="output-mode-switch",
                detail=request.detail,
            )
            raise RuntimeError(
                "output-mode graph did not reach two stable canonical readbacks"
            )

        spotify_stream_rate = None
        if request.source == "spotify" and request.should_play:
            source_rate = self._deps.coordinator_source_rate("spotify", request.target_track)
            spotify_stream_rate = await self._deps.wait_for_spotify_sink_input_samplerate(expected_rate=source_rate)
            if spotify_stream_rate != source_rate:
                raise RuntimeError(
                    "Spotify stream rate mismatch during output-mode commit: "
                    f"expected={source_rate} actual={spotify_stream_rate}"
                )

        if source_policy.is_mpv_source(request.source) and request.target_url:
            state = dict(self._player.state if self._player else {})
            if state.get("current_file") != request.target_url:
                raise RuntimeError(
                    "output-mode transition changed the loaded music source: "
                    f"expected={request.target_url} actual={state.get('current_file')}"
                )
            if request.should_play and (state.get("paused") or not state.get("playing")):
                raise RuntimeError("music transport did not resume for output-mode commit")
            if not request.should_play and not state.get("paused"):
                raise RuntimeError("music transport was not left paused for output-mode commit")
        elif request.source == "spotify":
            spotify_state = await self._deps.get_spotify_ui_state()
            if request.should_play and spotify_state.get("status") != "Playing":
                raise RuntimeError("Spotify did not resume for output-mode commit")
            if not request.should_play and spotify_state.get("status") == "Playing":
                raise RuntimeError("Spotify was not left paused for output-mode commit")
        elif request.source == "qobuz":
            qobuz_state = await self._deps.get_qobuz_ui_state()
            if request.should_play and qobuz_state.get("status") != "Playing":
                raise RuntimeError("Qobuz did not resume for output-mode commit")
            if not request.should_play and qobuz_state.get("status") == "Playing":
                raise RuntimeError("Qobuz was not left paused for output-mode commit")

        return {
            "committed": True,
            "output_mode_graph": True,
            "graph_complete": True,
            "graph_signature": signatures[-1],
            "active_rate": rate.get("active_rate"),
            "force_rate": rate.get("force_rate"),
            "spotify_stream_rate": spotify_stream_rate,
        }

    async def verify_committed_transition(self, request: TransitionRequest) -> dict[str, Any]:
        return await self._verify_transition(request, require_source_volume=True)

    async def evaluate_same_graph_fast_path(
        self, request: TransitionRequest, snapshot: Mapping[str, Any]
    ) -> bool:
        """Return whether a local play can switch inside the committed graph.

        Every caller of the Coordinator expects a semantically identical-graph
        track switch to stay transport-only: same mpv instance (ports persist),
        same sample rate, same output mode, healthy DSP and a complete
        canonical graph.  Any case that can change the graph or whose state is
        not provably safe falls back to the full transition.
        """
        if request.operation != "play":
            return False
        if request.source != "local":
            return False
        if not request.should_play:
            return False
        if request.rate_change or request.graph_only or request.output_mode_target:
            return False
        if request.recovery_commit_context_id or request.native_queue:
            return False
        if not request.reload_source:
            return False
        target_rate = request.target_rate
        if not isinstance(target_rate, int) or target_rate <= 0:
            return False
        if snapshot.get("active_rate") != target_rate:
            return False
        if snapshot.get("force_rate") not in {None, 0, target_rate}:
            return False
        current_track = dict(snapshot.get("current_track") or {})
        if current_track.get("source") != "local":
            return False
        player_state = dict(snapshot.get("player") or {})
        if not player_state.get("current_file") or player_state.get("ended"):
            return False
        if self._deps.measurement_audio_graph_owned():
            return False
        if not request.audio_overview:
            return False
        try:
            # One cheap diagnosis reusing the transition-frozen overview: the
            # canonical graph must still be fully wired (ingress, source and
            # output links, DSP active at the target rate, no bypass).
            diagnosis = await self._deps.playback_graph_diagnosis(
                audio_overview=dict(request.audio_overview),
                source="local",
                target_rate=target_rate,
                require_source=True,
            )
        except Exception as exc:
            logger.warning(
                "Same-graph fast-path graph check failed; using full transition: %s",
                exc,
            )
            return False
        return bool(diagnosis.get("links_complete"))

    async def verify_same_graph_commit(self, request: TransitionRequest) -> dict[str, Any]:
        """Lightweight commit readback for the same-graph fast path.

        The graph was verified healthy before the switch and nothing in this
        path mutates it, so only the player state is read back: the target
        must be loaded and audible at the committed volume.  The shared live
        IPC validation is the same contract as the standard commit verifier;
        the fast path never re-runs the graph diagnosis.
        """
        state = dict(self._player.state if self._player else {})
        live_mpv = self._live_mpv_commit_state(state)
        self._verify_mpv_live_commit(
            request,
            state,
            live_mpv,
            require_playing=True,
            require_source_volume=True,
            stage_label="fast-path commit",
        )
        return {
            "committed": True,
            "player": state,
            "fast_path": True,
            "graph_complete": True,
        }

