#!/usr/bin/env python3
"""Output selection commits only with the verified runtime graph."""

import asyncio
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import audio.samplerate.overview as output_overview
import measurement.session as measurement_session
from audio.output_state import default_output_state, set_mode_routing, switch_mode
from audio.samplerate import _load_audio_output_selection, _save_audio_output_selection
from dsp.orchestration import DspOrchestrator
from playback.transition import PlaybackTransitionCoordinator, TransitionRequest
from test_dsp_v2_sync_preference import _FakeDeps
from test_playback_transition_coordinator import FakeRuntime


class OutputSelectionTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.directory.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        _save_audio_output_selection("A")
        self.state = {"default": "A", "graph": "A"}
        self.coordinator = SimpleNamespace(lock=asyncio.Lock())
        self.rate_session = SimpleNamespace(lock=asyncio.Lock(), has_active_jobs=False)
        self.patches = [
            mock.patch.object(main, "playback_transition_coordinator", self.coordinator),
            mock.patch.object(main, "measurement_sr_session", self.rate_session),
            mock.patch.object(main, "get_audio_output_overview", side_effect=self.overview),
            mock.patch.object(main.samplerate, "_set_default_sink", side_effect=self.set_sink),
            mock.patch.object(output_overview, "_set_default_sink", side_effect=self.set_sink),
            mock.patch.object(output_overview, "get_audio_output_overview", side_effect=self.overview),
            mock.patch.object(main, "dsp_orchestrator", SimpleNamespace(
                sync_runtime=self.sync_graph,
                refresh_peak_monitor_after_effects_change=self.refresh_peak)),
            mock.patch.object(main.runtime, "dsp_runtime", SimpleNamespace(snapshot=self.snapshot)),
            mock.patch.object(main.playback_orchestration, "configured", return_value=SimpleNamespace(
                playback_graph_diagnosis=self.diagnose)),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def overview(self, *, selection_key=None):
        selected = selection_key or _load_audio_output_selection()["selected_key"]
        return {
            "outputs": [{"key": key, "name": key, "selectable": True,
                         "supported_rates": [48000], "channels": 2} for key in ("A", "B")],
            "selected_output": {"key": selected},
            "default_output": {"key": self.state["default"]},
            "output_mode": {"mode": "stereo", "effective_output_key": selected},
        }

    async def sync_graph(self, overview=None, **kwargs):
        target = (kwargs.get("target_overview") or overview or self.overview())["selected_output"]["key"]
        self.state["graph"] = target

    def set_sink(self, key):
        self.state["default"] = key

    async def refresh_peak(self, reason):
        return None

    def snapshot(self):
        return {"active": True, "config": {"output_key": self.state["graph"]}}

    async def diagnose(self, target):
        key = target["output_mode"]["effective_output_key"]
        return {"output_key": key, "output_links_complete": self.state["graph"] == key}

    async def select(self, key="B"):
        return await main.save_audio_output_selection_route(SimpleNamespace(json=lambda: self.body(key)))

    async def body(self, key):
        return {"key": key}

    def assert_old(self):
        self.assertEqual(self.state, {"default": "A", "graph": "A"})
        self.assertEqual(_load_audio_output_selection()["selected_key"], "A")

    async def test_persistence_failure_rolls_back_graph_and_sink(self):
        original = main.samplerate._save_audio_output_selection

        def fail_for_b(key):
            if key == "B":
                raise OSError("disk full")
            return original(key)

        with mock.patch.object(main.samplerate, "_save_audio_output_selection", side_effect=fail_for_b):
            with self.assertRaises(Exception):
                await self.select()
        self.assert_old()

    async def test_runtime_failure_never_commits_selection(self):
        async def fail_for_b(overview=None, **kwargs):
            target = (kwargs.get("target_overview") or overview)["selected_output"]["key"]
            if target == "B":
                raise RuntimeError("graph apply failed")
            await self.sync_graph(overview, **kwargs)

        with mock.patch.object(main.dsp_orchestrator, "sync_runtime", side_effect=fail_for_b):
            with self.assertRaises(Exception):
                await self.select()
        self.assert_old()

    async def test_failed_graph_readback_rolls_back(self):
        async def incomplete(target):
            key = target["selected_output"]["key"]
            return {"output_key": key, "output_links_complete": key == "A"}

        with mock.patch.object(main.playback_orchestration.configured(), "playback_graph_diagnosis", side_effect=incomplete):
            with self.assertRaises(Exception):
                await self.select()
        self.assert_old()

    async def test_partially_written_selection_is_restored(self):
        original = main.samplerate._save_audio_output_selection

        def write_then_fail(key):
            original(key)
            raise OSError("failed after write")

        with mock.patch.object(main.samplerate, "_save_audio_output_selection", side_effect=write_then_fail):
            with self.assertRaises(Exception):
                await self.select()
        self.assert_old()

    async def test_failed_apply_does_not_rebuild_unchanged_old_graph(self):
        async def unavailable(*_args, **_kwargs):
            raise RuntimeError("engine unavailable")

        with mock.patch.object(main.dsp_orchestrator, "sync_runtime", side_effect=unavailable):
            with self.assertRaises(HTTPException) as failure:
                await self.select()
        self.assertEqual(failure.exception.status_code, 500)
        self.assertIn("engine unavailable", failure.exception.detail)
        self.assert_old()

    async def test_rollback_uses_unique_successor_when_old_profile_is_gone(self):
        old = 'alsa_output.pci-card.hdmi-stereo'
        new = 'alsa_output.pci-card.hdmi-surround'
        _save_audio_output_selection(old)
        self.state.update(default=new, graph=old)

        def overview(*, selection_key=None):
            return {'outputs': [{'key': new, 'name': new, 'selectable': True, 'channels': 6}],
                    'selected_output': {'key': new}, 'default_output': {'key': new},
                    'output_mode': {'mode': 'surround', 'effective_output_key': new}}

        with mock.patch.object(main, 'get_audio_output_overview', side_effect=overview), \
                mock.patch.object(output_overview, 'get_audio_output_overview', side_effect=overview), \
                mock.patch.object(main.samplerate, '_save_audio_output_selection', side_effect=OSError('disk full')):
            with self.assertRaises(HTTPException) as failure:
                await self.select(new)
        self.assertIn('disk full', failure.exception.detail)
        self.assertNotIn('previous graph could not be restored', failure.exception.detail)
        self.assertEqual(self.state, {'default': new, 'graph': new})
        self.assertEqual(_load_audio_output_selection()['selected_key'], old)

    async def test_coordinator_transition_cannot_restore_a_after_b_commits(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def transition_a():
            async with self.coordinator.lock:
                entered.set()
                await release.wait()
                self.state["graph"] = "A"

        first = asyncio.create_task(transition_a())
        await entered.wait()
        selection = asyncio.create_task(self.select())
        await asyncio.sleep(0.05)
        self.assert_old()
        self.assertFalse(selection.done())
        release.set()
        await first
        await selection
        self.assertEqual(self.state, {"default": "B", "graph": "B"})
        self.assertEqual(_load_audio_output_selection()["selected_key"], "B")

    async def test_last_measurement_release_completes_with_queued_output_selection(self):
        release_entered = asyncio.Event()
        continue_release = asyncio.Event()
        selection_attempted = asyncio.Event()

        class ObservedLock(asyncio.Lock):
            async def acquire(self):
                selection_attempted.set()
                return await super().acquire()

        session = measurement_session.MeasurementSampleRateSession()
        session.lock = ObservedLock()
        session.active = True
        session.active_manual_job_ids.add("last-sweep")
        session._rate_changed = True
        await session.request_close()
        self.assertTrue(session.deferred_release_pending)

        runtime = FakeRuntime()
        runtime.rate = 48000
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        coordinator.lock = ObservedLock()
        track = {"source": "local", "id": "track-a", "url": "/music/current.flac"}
        snapshot = {
            **track, "track_info": track, "expected_rate": 44100,
            "was_playing": True, "position": 12.5,
        }

        async def validate_intent(**kwargs):
            self.assertTrue(session.lock.locked())
            self.assertEqual(kwargs["expected_id"], "track-a")
            release_entered.set()
            await continue_release.wait()
            return True

        async def sync_owned_graph(*args, **kwargs):
            self.assertTrue(session.lock.locked())
            self.assertTrue(coordinator.lock.locked())
            self.assertEqual(kwargs["ownership"], main.DspSyncOwnership.RATE)
            self.assertFalse(session.active)
            self.assertTrue(coordinator.last_result.committed)
            await self.sync_graph(*args, **kwargs)

        services = replace(
            main._make_measurement_services(),
            get_current_track_info=lambda: track,
            measurement_restore_intent_matches_live_state=validate_intent,
            begin_playback_transition_attempt=lambda: 1,
            end_playback_transition_attempt=lambda: None,
            stdin_measurement_release=None,
            post_release_input_resync=None,
            post_release_policy_rate_restore=None,
        )
        with mock.patch.object(main, "measurement_sr_session", session), mock.patch.object(
            main, "playback_transition_coordinator", coordinator
        ), mock.patch.object(measurement_session, "_services", services), mock.patch.object(
            measurement_session, "_playback_state_before_measurement", snapshot
        ), mock.patch.object(main.dsp_orchestrator, "sync_runtime", side_effect=sync_owned_graph):
            release_task = asyncio.create_task(session.unregister_manual_job("last-sweep"))
            selection_task = None
            try:
                await asyncio.wait_for(release_entered.wait(), 1)
                selection_attempted.clear()
                selection_task = asyncio.create_task(self.select())
                await asyncio.wait_for(selection_attempted.wait(), 1)
                self.assertFalse(release_task.done())
                self.assert_old()
                continue_release.set()
                _, pending = await asyncio.wait({release_task, selection_task}, timeout=2)
                self.assertFalse(pending, "Measurement release and output selection must both complete")
                await release_task
                with self.assertRaises(HTTPException) as busy:
                    await selection_task
                self.assertEqual(busy.exception.status_code, 423)
                self.assert_old()
                # Once release is complete, the same selection can commit normally.
                await self.select()
            finally:
                continue_release.set()
                tasks = [task for task in (release_task, selection_task) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            self.assertIsNone(measurement_session._playback_state_before_measurement)
        self.assertEqual(self.state, {"default": "B", "graph": "B"})
        self.assertEqual(_load_audio_output_selection()["selected_key"], "B")
        self.assertEqual(coordinator.last_result.source, "local")
        self.assertEqual(coordinator.last_result.target_rate, 44100)
        self.assertEqual(runtime.rate, 44100)
        self.assertEqual(runtime.current_file, "/music/current.flac")
        self.assertEqual(runtime.position, 12.5)
        self.assertTrue(runtime.playing)
        self.assertFalse(session.active)
        self.assertFalse(session.has_active_jobs)
        self.assertFalse(session.close_requested)
        self.assertFalse(session.deferred_release_pending)
        self.assertEqual(session.generation, 1)
        self.assertFalse(session.lock.locked())
        self.assertFalse(coordinator.lock.locked())

    async def test_playback_dsp_sync_completes_with_queued_output_selection(self):
        playback_entered = asyncio.Event()
        continue_playback = asyncio.Event()
        selection_waiting = asyncio.Event()
        graph_lock = asyncio.Lock()
        deps = _FakeDeps(v2_target=object())
        deps.get_measurement_sr_session = lambda: self.rate_session
        deps.graph_write_lock = lambda: graph_lock
        dsp = DspOrchestrator(deps)

        class ObservedCoordinatorLock(asyncio.Lock):
            async def acquire(self):
                if self.locked():
                    selection_waiting.set()
                return await super().acquire()

        class PlaybackRuntime(FakeRuntime):
            async def read_transition_snapshot(self, request):
                playback_entered.set()
                await continue_playback.wait()
                return await super().read_transition_snapshot(request)

            async def establish_effects_and_helper(self, request):
                # Normal playback must take the real session and graph locks.
                await dsp.sync_runtime(reason="coordinator-play")
                return await super().establish_effects_and_helper(request)

        runtime = PlaybackRuntime()
        runtime.rate = 48000
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        coordinator.lock = ObservedCoordinatorLock()
        request = TransitionRequest(operation="play", source="local", target_rate=48000,
                                    target_url="/music/current.flac", should_play=True)
        with mock.patch.object(main, "playback_transition_coordinator", coordinator):
            playback_task = asyncio.create_task(coordinator.execute(request))
            selection_task = None
            try:
                await asyncio.wait_for(playback_entered.wait(), 1)
                selection_task = asyncio.create_task(self.select())
                await asyncio.wait_for(selection_waiting.wait(), 1)
                continue_playback.set()
                done, pending = await asyncio.wait({playback_task, selection_task}, timeout=2)
                self.assertFalse(pending, "Playback DSP sync and output selection must both complete")
                await asyncio.gather(*done)
            finally:
                continue_playback.set()
                tasks = [task for task in (playback_task, selection_task) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        self.assertTrue(coordinator.last_result.committed)
        self.assertTrue(runtime.playing)
        self.assertEqual(deps.runtime.rendered_calls, [deps._v2_target])
        self.assertEqual(self.state, {"default": "B", "graph": "B"})
        self.assertEqual(_load_audio_output_selection()["selected_key"], "B")
        self.assertFalse(self.rate_session.lock.locked())
        self.assertFalse(coordinator.lock.locked())
        self.assertFalse(graph_lock.locked())

    async def test_output_selection_refuses_session_lock_with_pending_handoff(self):
        session_waiting = asyncio.Event()

        class ObservedSessionLock(asyncio.Lock):
            async def acquire(self):
                if self.locked():
                    session_waiting.set()
                return await super().acquire()

        lock = self.rate_session.lock = ObservedSessionLock()

        async def finish_session():
            async with lock, self.coordinator.lock:
                return None

        await lock.acquire()
        task = asyncio.create_task(finish_session())
        try:
            await asyncio.wait_for(session_waiting.wait(), 1)
            # Release has woken the waiter, but it has not claimed the lock yet.
            lock.release()
            self.assertFalse(lock.locked())
            async with asyncio.timeout(1):
                with self.assertRaises(HTTPException) as busy:
                    await self.select()
            self.assertEqual(busy.exception.status_code, 423)
            await asyncio.wait_for(task, 1)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assert_old()
        self.assertFalse(lock.locked())
        self.assertFalse(self.coordinator.lock.locked())

    async def test_cancelled_switch_never_releases_ownership_mid_apply(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        original = self.sync_graph

        async def paused_sync(overview=None, **kwargs):
            target = (kwargs.get("target_overview") or overview)["selected_output"]["key"]
            if target == "B":
                entered.set()
                await release.wait()
            await original(overview, **kwargs)

        with mock.patch.object(main.dsp_orchestrator, "sync_runtime", side_effect=paused_sync):
            task = asyncio.create_task(self.select())
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertTrue(self.coordinator.lock.locked())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.state["default"], self.state["graph"])
        self.assertEqual(_load_audio_output_selection()["selected_key"], self.state["graph"])


class StagedOverviewTests(unittest.TestCase):
    def test_target_uses_new_device_without_persisting_selection(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": directory}
        ):
            _save_audio_output_selection("alsa_output.A")

            head = switch_mode(set_mode_routing(
                set_mode_routing(default_output_state(), "stereo-sub", "alsa_output.A",
                                 ["main_l", "main_r", "sub1", "off"]),
                "stereo-sub", "alsa_output.B", ["main_l", "main_r"]), "stereo-sub")

            def run(command):
                if command[:4] == ["pactl", "list", "sinks", "short"]:
                    return ("1\talsa_output.A\tPipeWire\ts16le 4ch 48000Hz\tRUNNING\n"
                            "2\talsa_output.B\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED\n")
                return ""

            status = {"available": True, "sink": {"name": "alsa_output.A"},
                      "relevant_sink": {"name": "alsa_output.A"}, "notes": []}
            with mock.patch.object(output_overview, "_run_command", side_effect=run), mock.patch.object(
                output_overview, "get_bluetooth_audio_overview", return_value={"available": False}
            ), mock.patch.object(output_overview, "_output_state_head_loader", return_value=head
            ):
                target = output_overview.get_audio_output_overview(status, selection_key="alsa_output.B")
            self.assertEqual(target["selected_output"]["key"], "alsa_output.B")
            self.assertEqual(target["output_mode"]["effective_output_key"], "alsa_output.B")
            self.assertEqual(target["default_output"]["key"], "alsa_output.A")
            self.assertEqual(target["output_mode"]["mode"], "stereo")
            self.assertEqual(_load_audio_output_selection()["selected_key"], "alsa_output.A")


if __name__ == "__main__":
    unittest.main()
