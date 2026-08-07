#!/usr/bin/env python3
"""Focused post-source-start production-graph reconciliation contracts."""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest.mock import AsyncMock, call, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import main
from playback_transition import PlaybackTransitionCoordinator, PlaybackTransitionFailure, TransitionRequest
from playback_transition_test_support import MainCoreTransitionRuntime


OUTPUT_KEY = "alsa_output.pci-0000_00_1f.3.analog-stereo"
EE_LEFT = "ee_soe_output_level:output_FL"
EE_RIGHT = "ee_soe_output_level:output_FR"
HELPER_LEFT = "fxroute_21_stage1:input_L"
HELPER_RIGHT = "fxroute_21_stage1:input_R"


def _graph_snapshot(*, missing: tuple[str, ...] = (), signature: str = "complete") -> dict:
    links = {
        f"{EE_LEFT} -> {HELPER_LEFT}": f"{EE_LEFT} -> {HELPER_LEFT}" not in missing,
        f"{EE_RIGHT} -> {HELPER_RIGHT}": f"{EE_RIGHT} -> {HELPER_RIGHT}" not in missing,
        f"fxroute_21_stage1:output_1 -> {OUTPUT_KEY}:playback_FL": True,
        f"fxroute_21_stage1:output_2 -> {OUTPUT_KEY}:playback_FR": True,
        f"fxroute_21_stage1:output_3 -> {OUTPUT_KEY}:playback_RL": True,
        f"fxroute_21_stage1:output_4 -> {OUTPUT_KEY}:playback_RR": True,
    }
    return {
        "mode": "subwoofer-2.2",
        "output_key": OUTPUT_KEY,
        "ee_ports": True,
        "helper_ports": True,
        "helper_active": True,
        "helper_rate": 48000,
        "helper_rate_matches": True,
        "source_links": {
            "mpv:output_FL -> easyeffects_sink:playback_FL": True,
            "mpv:output_FR -> easyeffects_sink:playback_FR": True,
        },
        "source_links_complete": True,
        "direct_ee_to_hw_present": False,
        "links": links,
        "links_complete": all(links.values()),
        "bypass_only": False,
        "port_identities": {
            "source": ("mpv:output_FL", "mpv:output_FR"),
            "ee": (EE_LEFT, EE_RIGHT),
            "helper": (
                HELPER_LEFT,
                HELPER_RIGHT,
                "fxroute_21_stage1:output_1",
                "fxroute_21_stage1:output_2",
                "fxroute_21_stage1:output_3",
                "fxroute_21_stage1:output_4",
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
            generation=main.playback_transition_generation,
            use_core=False,
            events=events,
        )

        async def reconcile(request):
            events.append("post-start-graph-reconcile")
            return await main._coordinator_reconcile_post_start_graph(request)

        runtime.reconcile_post_start_graph = reconcile
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        diagnosis = AsyncMock(side_effect=diagnoses)
        relink = AsyncMock()
        with patch.object(main, "get_samplerate_status", return_value={
            "active_rate": 48000,
            "force_rate": 48000,
        }), patch.object(main, "_playback_graph_diagnosis", diagnosis), patch.object(
            main, "_connect_ports", relink
        ):
            result = await coordinator.execute(_request())
        return result, coordinator, runtime, diagnosis, relink

    async def test_missing_post_start_links_are_relinked_and_two_readbacks_allow_commit(self):
        initial = _graph_snapshot(
            missing=(f"{EE_LEFT} -> {HELPER_LEFT}", f"{EE_RIGHT} -> {HELPER_RIGHT}"),
            signature="missing-ee-helper",
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
                call((EE_LEFT,), HELPER_LEFT),
                call((EE_RIGHT,), HELPER_RIGHT),
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
            missing=(f"{EE_LEFT} -> {HELPER_LEFT}",),
            signature="missing-ee-helper",
        )
        stable_once = _graph_snapshot(signature="stable-once")
        disappeared = _graph_snapshot(
            missing=(f"{EE_RIGHT} -> {HELPER_RIGHT}",),
            signature="link-disappeared-again",
        )

        events: list[str] = []
        runtime = MainCoreTransitionRuntime(
            target_rate=48000,
            generation=main.playback_transition_generation,
            use_core=False,
            events=events,
        )

        async def reconcile(request):
            events.append("post-start-graph-reconcile")
            return await main._coordinator_reconcile_post_start_graph(request)

        runtime.reconcile_post_start_graph = reconcile
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        diagnosis = AsyncMock(side_effect=[initial, stable_once, disappeared])
        relink = AsyncMock()
        with patch.object(main, "get_samplerate_status", return_value={
            "active_rate": 48000,
            "force_rate": 48000,
        }), patch.object(main, "_playback_graph_diagnosis", diagnosis), patch.object(
            main, "_connect_ports", relink
        ):
            with self.assertRaises(PlaybackTransitionFailure) as caught:
                await coordinator.execute(_request())

        self.assertEqual(caught.exception.stage, "post-start-graph-reconcile")
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertIsNone(coordinator.last_result)
        self.assertNotIn("graph-readback", runtime.events)
        self.assertNotIn("commit-readback", runtime.events)
        relink.assert_awaited_once_with((EE_LEFT,), HELPER_LEFT)


if __name__ == "__main__":
    unittest.main()
