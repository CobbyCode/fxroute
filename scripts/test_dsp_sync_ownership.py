#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""DSP sync preserves rate-before-graph exclusion and delegated ownership."""

import asyncio
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from dsp import locks
from dsp.orchestration import DspOrchestrator
from playback.orchestration import PlaybackOrchestrator
from playback.runtime import FxrouteTransitionRuntime
from playback.transition import TransitionRequest
from test_dsp_v2_sync_preference import _FakeDeps


class DspSyncOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rate = asyncio.Lock()
        self.graph = asyncio.Lock()
        self.deps = _FakeDeps(v2_target=object())
        self.deps.get_measurement_sr_session = lambda: SimpleNamespace(lock=self.rate)
        self.deps.graph_write_lock = lambda: self.graph
        self.orchestrator = DspOrchestrator(self.deps)

    async def test_unowned_sync_waits_for_rate_before_taking_graph(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def apply(_target):
            self.assertTrue(self.rate.locked())
            self.assertTrue(self.graph.locked())
            entered.set()
            await release.wait()

        self.deps.runtime.sync_rendered = apply
        async with self.rate:
            sync = asyncio.create_task(self.orchestrator.sync_runtime(
                ownership=locks.DspSyncOwnership.UNOWNED))
            try:
                done, _ = await asyncio.wait({sync}, timeout=0.02)
                self.assertFalse(done)
                self.assertFalse(self.graph.locked())
            except BaseException:
                sync.cancel()
                await asyncio.gather(sync, return_exceptions=True)
                raise
        try:
            await asyncio.wait_for(entered.wait(), 1)
            contender = asyncio.create_task(self.graph.acquire())
            try:
                done, _ = await asyncio.wait({contender}, timeout=0.02)
                self.assertFalse(done, "Graph ownership must cover native apply")
            finally:
                contender.cancel()
                await asyncio.gather(contender, return_exceptions=True)
        finally:
            release.set()
            await asyncio.wait_for(sync, 1)
        self.assertFalse(self.rate.locked())
        self.assertFalse(self.graph.locked())

    async def test_delegated_rate_owner_still_acquires_graph(self):
        async with self.rate, self.graph:
            sync = asyncio.create_task(self.orchestrator.sync_runtime(
                ownership=locks.DspSyncOwnership.RATE))
            done, _ = await asyncio.wait({sync}, timeout=0.02)
            self.assertFalse(done)
            self.assertEqual(self.deps.runtime.rendered_calls, [])
            self.graph.release()
            try:
                await asyncio.wait_for(sync, 1)
                self.assertTrue(self.rate.locked())
                self.assertFalse(self.graph.locked())
            finally:
                await self.graph.acquire()
        self.assertEqual(self.deps.runtime.rendered_calls, [self.deps._v2_target])

    async def test_delegated_graph_owner_does_not_reacquire_or_release_parent_locks(self):
        async with self.rate, self.graph:
            await asyncio.wait_for(self.orchestrator.sync_runtime(
                ownership=locks.DspSyncOwnership.RATE_AND_GRAPH), 1)
            self.assertTrue(self.rate.locked())
            self.assertTrue(self.graph.locked())
        self.assertEqual(self.deps.runtime.rendered_calls, [self.deps._v2_target])

    async def test_measurement_release_uses_its_callers_rate_ownership(self):
        async with self.rate:
            await asyncio.wait_for(self.orchestrator.sync_runtime_at_rate(48000), 1)
            self.assertTrue(self.rate.locked())
        self.assertEqual(self.deps.runtime.rendered_calls,
                         [self.deps._v2_target, self.deps._v2_target])

    async def test_owned_sync_failure_keeps_the_callers_locks(self):
        self.deps.runtime.sync_rendered = AsyncMock(side_effect=RuntimeError("apply failed"))
        async with self.rate, self.graph:
            with self.assertRaisesRegex(RuntimeError, "apply failed"):
                await asyncio.wait_for(self.orchestrator.sync_runtime(
                    ownership=locks.DspSyncOwnership.RATE_AND_GRAPH), 1)
            self.assertTrue(self.rate.locked())
            self.assertTrue(self.graph.locked())

    async def test_cancelled_sync_releases_only_its_own_locks(self):
        entered = asyncio.Event()

        async def apply(_target):
            entered.set()
            await asyncio.Event().wait()

        self.deps.runtime.sync_rendered = apply
        async with self.rate:
            sync = asyncio.create_task(self.orchestrator.sync_runtime(
                ownership=locks.DspSyncOwnership.RATE))
            await asyncio.wait_for(entered.wait(), 1)
            sync.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await sync
            self.assertTrue(self.rate.locked())
            self.assertFalse(self.graph.locked())

    async def test_src_cleanup_borrows_configuration_even_without_src_request_fields(self):
        volume, mutation = asyncio.Lock(), asyncio.Lock()
        stage = PlaybackOrchestrator(SimpleNamespace(
            dsp_port_timeout_ms=1, get_audio_output_overview=self.deps.get_audio_output_overview,
            get_dsp_snapshot=self.deps.runtime.snapshot,
            helper_argument_sample_rate=lambda _snapshot: None,
            sync_runtime=self.orchestrator.sync_runtime, output_mode_subwoofer_modes=set()))
        stage.playback_graph_diagnosis = AsyncMock(return_value={
            "dsp_ports": False, "helper_ports": True, "links": {}, "links_complete": True})
        stage.wait_for_dsp_output_ports = AsyncMock(return_value=True)
        stage.repair_stereo_output_links_once = AsyncMock()
        adapter = FxrouteTransitionRuntime(SimpleNamespace(
            canonical_volume_write_lock=lambda: volume, dsp_mutation_lock=lambda: mutation,
            audio_configuration_lock=lambda: self.rate, guard_src_change=AsyncMock(),
            coordinator_establish_effects_and_helper=stage.establish_effects_and_helper))
        request = TransitionRequest(operation="sample-rate-policy", source="local",
                                    target_rate=48000, src_mode="native")
        with self.assertRaisesRegex(RuntimeError, "transition failed"):
            async with adapter.transition_configuration(request):
                # The coordinator drains this child before releasing configuration.
                await asyncio.wait_for(adapter.establish_effects_and_helper(
                    replace(request, operation="recovery", src_mode=None)), 1)
                self.assertTrue(all(lock.locked() for lock in (volume, mutation, self.rate)))
                raise RuntimeError("transition failed")
        self.assertFalse(any(lock.locked() for lock in (volume, mutation, self.rate, self.graph)))
        self.assertEqual(len(self.deps.runtime.rendered_calls), 2)

        # A subsequent unowned helper must not retain the failed transition's bypass.
        async with self.rate:
            sync = asyncio.create_task(adapter.establish_effects_and_helper(
                replace(request, operation="play", src_mode=None)))
            done, _ = await asyncio.wait({sync}, timeout=0.02)
            self.assertFalse(done)
        await asyncio.wait_for(sync, 1)
        self.assertEqual(len(self.deps.runtime.rendered_calls), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
