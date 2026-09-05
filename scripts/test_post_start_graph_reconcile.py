#!/usr/bin/env python3
"""Focused post-source-start production-graph reconciliation contracts."""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import main
import audio.pw_link as pw_link_mod
import playback.orchestration as playback_orchestration
from playback.transition import PlaybackTransitionCoordinator, PlaybackTransitionFailure, TransitionRequest
from playback_transition_test_support import MainCoreTransitionRuntime


OUTPUT_KEY = "alsa_output.pci-0000_00_1f.3.analog-stereo"
DSP_INGRESS_LEFT = "fxroute_dsp_sink:monitor_FL"
DSP_INGRESS_RIGHT = "fxroute_dsp_sink:monitor_FR"
HELPER_LEFT = "fxroute_dsp:input_1"
HELPER_RIGHT = "fxroute_dsp:input_2"


def _graph_snapshot(*, missing: tuple[str, ...] = (), signature: str = "complete") -> dict:
    links = {
        f"{DSP_INGRESS_LEFT} -> {HELPER_LEFT}": f"{DSP_INGRESS_LEFT} -> {HELPER_LEFT}" not in missing,
        f"{DSP_INGRESS_RIGHT} -> {HELPER_RIGHT}": f"{DSP_INGRESS_RIGHT} -> {HELPER_RIGHT}" not in missing,
        f"fxroute_dsp:output_1 -> {OUTPUT_KEY}:playback_FL": True,
        f"fxroute_dsp:output_2 -> {OUTPUT_KEY}:playback_FR": True,
        f"fxroute_dsp:output_3 -> {OUTPUT_KEY}:playback_RL": True,
        f"fxroute_dsp:output_4 -> {OUTPUT_KEY}:playback_RR": True,
    }
    return {
        "mode": "subwoofer-2.2",
        "output_key": OUTPUT_KEY,
        "dsp_ports": True,
        "helper_ports": True,
        "helper_active": True,
        "helper_rate": 48000,
        "helper_rate_matches": True,
        "source_links": {
            "mpv:output_FL -> fxroute_dsp_sink:playback_FL": True,
            "mpv:output_FR -> fxroute_dsp_sink:playback_FR": True,
        },
        "source_links_complete": True,
        "links": links,
        "links_complete": all(links.values()),
        "bypass_only": False,
        "port_identities": {
            "source": ("mpv:output_FL", "mpv:output_FR"),
            "dsp": (DSP_INGRESS_LEFT, DSP_INGRESS_RIGHT),
            "helper": (
                HELPER_LEFT,
                HELPER_RIGHT,
                "fxroute_dsp:output_1",
                "fxroute_dsp:output_2",
                "fxroute_dsp:output_3",
                "fxroute_dsp:output_4",
            ),
            "output": (
                f"{OUTPUT_KEY}:playback_FL",
                f"{OUTPUT_KEY}:playback_FR",
                f"{OUTPUT_KEY}:playback_RL",
                f"{OUTPUT_KEY}:playback_RR",
            ),
        },
        "signature": signature,
    }


def _request() -> TransitionRequest:
    return TransitionRequest(
        operation="play",
        source="local",
        target_rate=48000,
        target_url="/music/target.flac",
        target_track={"source": "local", "url": "/music/target.flac"},
        should_play=True,
        rate_change=False,
        reload_source=True,
    )


class PostStartGraphReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def _run_with_real_coordinator_hook(self, diagnoses: list[dict]):
        events: list[str] = []
        runtime = MainCoreTransitionRuntime(
            target_rate=48000,
            generation=main.playback_state.playback_transition_epoch,
            use_core=False,
            events=events,
        )

        async def reconcile(request):
            events.append("post-start-graph-reconcile")
            return await playback_orchestration.configured().reconcile_post_start_graph(request)

        runtime.reconcile_post_start_graph = reconcile
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        diagnosis = AsyncMock(side_effect=diagnoses)
        relink = AsyncMock()
        with patch.object(main, "get_samplerate_status", return_value={
            "active_rate": 48000,
            "force_rate": 48000,
        }), patch.object(playback_orchestration.configured(), "playback_graph_diagnosis", diagnosis), patch.object(
            pw_link_mod, "connect_ports", relink
        ):
            result = await coordinator.execute(_request())
        return result, coordinator, runtime, diagnosis, relink

    async def test_missing_post_start_links_are_relinked_and_two_readbacks_allow_commit(self):
        initial = _graph_snapshot(
            missing=(f"{DSP_INGRESS_LEFT} -> {HELPER_LEFT}", f"{DSP_INGRESS_RIGHT} -> {HELPER_RIGHT}"),
            signature="missing-dsp-helper",
        )
        stable = _graph_snapshot(signature="stable-canonical")

        result, coordinator, runtime, diagnosis, relink = await self._run_with_real_coordinator_hook(
            [initial, stable, stable]
        )

        self.assertTrue(result.committed)
        self.assertFalse(coordinator.gate.closed)
        self.assertEqual(diagnosis.await_count, 3)
        self.assertEqual(
            relink.await_args_list,
            [
                call((DSP_INGRESS_LEFT,), HELPER_LEFT),
                call((DSP_INGRESS_RIGHT,), HELPER_RIGHT),
            ],
        )
        self.assertLess(
            runtime.events.index("start"),
            runtime.events.index("post-start-graph-reconcile"),
        )
        self.assertLess(
            runtime.events.index("post-start-graph-reconcile"),
            runtime.events.index("graph-readback"),
        )

    async def test_links_disappearing_after_relink_latch_failure_and_do_not_commit(self):
        initial = _graph_snapshot(
            missing=(f"{DSP_INGRESS_LEFT} -> {HELPER_LEFT}",),
            signature="missing-dsp-helper",
        )
        stable_once = _graph_snapshot(signature="stable-once")
        disappeared = _graph_snapshot(
            missing=(f"{DSP_INGRESS_RIGHT} -> {HELPER_RIGHT}",),
            signature="link-disappeared-again",
        )

        events: list[str] = []
        runtime = MainCoreTransitionRuntime(
            target_rate=48000,
            generation=main.playback_state.playback_transition_epoch,
            use_core=False,
            events=events,
        )

        async def reconcile(request):
            events.append("post-start-graph-reconcile")
            return await playback_orchestration.configured().reconcile_post_start_graph(request)

        runtime.reconcile_post_start_graph = reconcile
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        diagnosis = AsyncMock(side_effect=[initial, stable_once, disappeared])
        relink = AsyncMock()
        with patch.object(main, "get_samplerate_status", return_value={
            "active_rate": 48000,
            "force_rate": 48000,
        }), patch.object(playback_orchestration.configured(), "playback_graph_diagnosis", diagnosis), patch.object(
            pw_link_mod, "connect_ports", relink
        ):
            with self.assertRaises(PlaybackTransitionFailure) as caught:
                await coordinator.execute(_request())

        self.assertEqual(caught.exception.stage, "post-start-graph-reconcile")
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertIsNone(coordinator.last_result)
        self.assertNotIn("graph-readback", runtime.events)
        self.assertNotIn("commit-readback", runtime.events)
        relink.assert_awaited_once_with((DSP_INGRESS_LEFT,), HELPER_LEFT)


def _paused_qobuz_output_mode_request(*, should_play=False) -> TransitionRequest:
    """Output-mode switch with a paused-but-committed Qobuz owner.

    Mirrors the live failure: the owner keeps a track id (pausing keeps the
    owner by design) while qbzd suspends its stream, so no source ports
    exist. Only ``should_play`` differs between the paused and playing
    variants; ``target_url`` is set in both, as the route builds it.
    """
    return TransitionRequest(
        operation="output-mode-switch",
        source="qobuz",
        target_rate=44100,
        target_url="351323754",
        target_track={"source": "qobuz", "id": "351323754"},
        should_play=should_play,
        rate_change=False,
        reload_source=False,
        detail="api-audio-output-mode",
        output_mode_target={
            "output_mode": {"mode": "stereo", "effective_output_key": OUTPUT_KEY},
        },
        output_mode_config={"mode": "stereo"},
    )


def _stereo_diagnosis_without_source_links() -> dict:
    links = {
        f"{DSP_INGRESS_LEFT} -> {HELPER_LEFT}": True,
        f"{DSP_INGRESS_RIGHT} -> {HELPER_RIGHT}": True,
        f"fxroute_dsp:output_1 -> {OUTPUT_KEY}:playback_FL": True,
        f"fxroute_dsp:output_2 -> {OUTPUT_KEY}:playback_FR": True,
    }
    return {
        "mode": "stereo",
        "output_key": OUTPUT_KEY,
        "dsp_ports": True,
        "helper_ports": True,
        "helper_active": True,
        "helper_rate": 44100,
        "helper_rate_matches": True,
        # require_source=False yields None (not False): a paused source has
        # no stream ports by design, and that must not poison the verdict.
        "source_links": {},
        "source_links_complete": None,
        "links": links,
        "links_complete": True,
        "bypass_only": False,
        "port_identities": {
            "source": (),
            "dsp": (DSP_INGRESS_LEFT, DSP_INGRESS_RIGHT),
            "helper": (HELPER_LEFT, HELPER_RIGHT, "fxroute_dsp:output_1", "fxroute_dsp:output_2"),
            "output": (f"{OUTPUT_KEY}:playback_FL", f"{OUTPUT_KEY}:playback_FR"),
        },
        "signature": "paused-qobuz-stereo",
    }


def _diagnosis_side_effect(*, missing_source: bool):
    """Production-faithful diagnosis: source verdicts only when required."""

    async def diagnosis(overview, source=None, require_source=False, **kwargs):
        base = _stereo_diagnosis_without_source_links()
        if require_source and missing_source:
            return {
                **base,
                "source_links": {
                    "alsa_playback.qbzd:output_FL -> fxroute_dsp_sink:playback_FL": False,
                    "alsa_playback.qbzd:output_FR -> fxroute_dsp_sink:playback_FR": False,
                },
                "source_links_complete": False,
                "links_complete": False,
                "signature": "suspended-source-required",
            }
        return base

    return diagnosis


class OutputModePausedSourceTests(unittest.IsolatedAsyncioTestCase):
    """An output-mode switch must not demand stream ports of a paused source.

    Regression: switching into stereo with a paused-but-committed Qobuz
    owner failed at post-start-graph-reconcile because the request still
    carried the owner's track id, which pulled the (suspended) source
    stream into the expected graph. A mode switch never (re)starts its
    source, so only a playing source contributes stream links.
    """

    async def test_reconcile_ignores_suspended_source_on_paused_switch(self):
        request = _paused_qobuz_output_mode_request(should_play=False)
        orchestrator = playback_orchestration.configured()
        with patch.object(
            orchestrator,
            "playback_graph_diagnosis",
            AsyncMock(side_effect=_diagnosis_side_effect(missing_source=True)),
        ):
            result = await orchestrator.reconcile_post_start_graph(request)
        self.assertTrue(result["graph_complete"])

    async def test_reconcile_still_requires_live_source_when_playing(self):
        request = _paused_qobuz_output_mode_request(should_play=True)
        orchestrator = playback_orchestration.configured()
        with patch.object(
            orchestrator,
            "playback_graph_diagnosis",
            AsyncMock(side_effect=_diagnosis_side_effect(missing_source=True)),
        ):
            with self.assertRaises(RuntimeError):
                await orchestrator.reconcile_post_start_graph(request)

    async def test_finalize_ignores_suspended_source_on_paused_switch(self):
        from playback.runtime.output_mode import _RuntimeOutputModeMixin

        request = _paused_qobuz_output_mode_request(should_play=False)

        # Mirror the production diagnosis: source verdicts only exist when
        # required; a suspended stream otherwise poisons nothing.
        async def diagnosis(overview, source=None, require_source=False, **kwargs):
            base = _stereo_diagnosis_without_source_links()
            if require_source:
                return {
                    **base,
                    "source_links": {
                        "alsa_playback.qbzd:output_FL -> fxroute_dsp_sink:playback_FL": False,
                        "alsa_playback.qbzd:output_FR -> fxroute_dsp_sink:playback_FR": False,
                    },
                    "source_links_complete": False,
                    "links_complete": False,
                }
            return base

        class Harness(_RuntimeOutputModeMixin):
            def __init__(self, deps):
                self._deps = deps

        harness = Harness(Mock())
        harness._deps.playback_graph_diagnosis = AsyncMock(side_effect=diagnosis)
        result = await harness.finalize_output_mode_graph_after_gate_open(request)
        self.assertTrue(result["graph_complete"])
        for _, kwargs in harness._deps.playback_graph_diagnosis.await_args_list:
            self.assertFalse(kwargs.get("require_source"))

    async def test_finalize_still_requires_live_source_when_playing(self):
        from playback.runtime.output_mode import _RuntimeOutputModeMixin

        request = _paused_qobuz_output_mode_request(should_play=True)
        diagnosis = {
            **_stereo_diagnosis_without_source_links(),
            "source_links": {
                "alsa_playback.qbzd:output_FL -> fxroute_dsp_sink:playback_FL": False,
                "alsa_playback.qbzd:output_FR -> fxroute_dsp_sink:playback_FR": False,
            },
            "source_links_complete": False,
            "links_complete": False,
        }

        class Harness(_RuntimeOutputModeMixin):
            def __init__(self, deps):
                self._deps = deps

        harness = Harness(Mock())
        harness._deps.playback_graph_diagnosis = AsyncMock(return_value=diagnosis)
        with self.assertRaises(RuntimeError):
            await harness.finalize_output_mode_graph_after_gate_open(request)


    async def test_verify_ignores_suspended_source_on_paused_switch(self):
        from playback.runtime.verification import _RuntimeVerificationMixin

        request = _paused_qobuz_output_mode_request(should_play=False)

        class Harness(_RuntimeVerificationMixin):
            def __init__(self, deps):
                self._deps = deps

        deps = Mock()
        deps.get_samplerate_status = Mock(
            return_value={"active_rate": 44100, "force_rate": 44100}
        )
        deps.playback_graph_diagnosis = AsyncMock(
            side_effect=_diagnosis_side_effect(missing_source=True)
        )
        deps.get_qobuz_ui_state = AsyncMock(return_value={"status": "Paused"})
        harness = Harness(deps)
        result = await harness.verify_output_mode_runtime(request)
        self.assertTrue(result["committed"])
        for _, kwargs in deps.playback_graph_diagnosis.await_args_list:
            self.assertFalse(kwargs.get("require_source"))

    async def test_verify_still_requires_live_source_when_playing(self):
        from playback.runtime.verification import _RuntimeVerificationMixin

        request = _paused_qobuz_output_mode_request(should_play=True)

        class Harness(_RuntimeVerificationMixin):
            def __init__(self, deps):
                self._deps = deps

        deps = Mock()
        deps.get_samplerate_status = Mock(
            return_value={"active_rate": 44100, "force_rate": 44100}
        )
        deps.playback_graph_diagnosis = AsyncMock(
            side_effect=_diagnosis_side_effect(missing_source=True)
        )
        deps.get_qobuz_ui_state = AsyncMock(return_value={"status": "Playing"})
        harness = Harness(deps)
        with self.assertRaises(RuntimeError):
            await harness.verify_output_mode_runtime(request)


if __name__ == "__main__":
    unittest.main()
