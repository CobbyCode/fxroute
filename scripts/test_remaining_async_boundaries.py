#!/usr/bin/env python3
"""Blocking discovery stays off the loop at measurement/playback boundaries."""

import asyncio
import inspect
import sys
import threading
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import dsp.api as dsp_api
import measurement.session as session
import measurement.spl_calibration as spl
import measurement.level_match_api as level_api
import measurement.polarity_check_api as polarity_api
from measurement.audio import MeasurementAudioAdapter
from measurement.speaker_sweep import JobSlot
from measurement.store import MeasurementStore
from playback.queue import PlaybackQueue
from measurement.autosub.jobs import _register_autosub_release_adapter
from measurement.autosub.release import create_release_adapter
from measurement.speaker_commit import create_speaker_release_adapter
from test_measurement_speaker_service import ServiceFixture


class AsyncBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.loop_thread = threading.get_ident()
        self.seen = []

    def probe(self, result):
        def read(*args, **kwargs):
            self.seen.append(threading.get_ident())
            return result
        return read

    def assert_off_loop(self):
        self.assertTrue(self.seen)
        self.assertNotIn(self.loop_thread, self.seen)

    async def test_manual_and_repeat_stage_after_registration_off_loop(self):
        store = SimpleNamespace(start_measurement=AsyncMock(return_value={"id": "single"}),
                                start_lr_repeat_measurement=AsyncMock(return_value={"id": "repeat"}))
        async def registered(rate, factory, **kwargs):
            return await factory()
        services = SimpleNamespace(get_store=lambda: store, get_session=lambda: None,
                                   auto_sub_active=lambda: False, stage_bank_v2_context=self.probe({}))
        with patch.object(session, "_measurement_services", return_value=services), \
                patch.object(session, "_start_registered_manual_measurement", registered), \
                patch.object(session, "_resolve_measurement_start_sample_rate", self.probe(48000)):
            for start in (session.start_measurement, session.start_lr_repeat_measurement):
                result = await start(input_id="mic", calibration_file=None,
                                     measurement_bank="global", measurement_group="")
                self.assertEqual(result["status"], "ok")
        self.assert_off_loop()

    async def test_store_target_freezing_off_loop(self):
        store = MeasurementStore.__new__(MeasurementStore)
        store._prepare_measurement_job_setup = AsyncMock(return_value={
            "job": {"id": "take", "created_at": "2026-10-15T12:00:00"}, "channel": "left"})
        store._freeze_measurement_job_target = self.probe({})
        store._freeze_repeat_job_target = self.probe({})
        store._register_measurement_job = lambda job, execute: job
        with patch("measurement.store.freeze_expected_native_context", return_value={}):
            self.assertEqual((await store.start_measurement(input_id="mic", channel="left"))["id"], "take")
            self.assertEqual((await store.start_lr_repeat_measurement(input_id="mic"))["id"], "take")
        self.assert_off_loop()

    async def test_commit_gate_verifier_off_loop(self):
        deps = SimpleNamespace(verify_measurement_commit=self.probe(None))
        with patch.object(dsp_api, "_deps", return_value=deps):
            result = dsp_api._verify_measurement_commit([{"mode": "stereo"}], {"mode": "stereo"})
            if inspect.isawaitable(result):
                await result
        self.assert_off_loop()

    async def test_queue_navigation_and_shuffle_rate_check_off_loop(self):
        requests = []
        async def transition(request):
            requests.append(request)
            return SimpleNamespace(committed=True, target_rate=48000)
        deps = SimpleNamespace(
            player=lambda: SimpleNamespace(state={}), transition_is_active=lambda: False,
            coordinator_target_rate=lambda *args: 48000, coordinator_rate_change=self.probe(True),
            run_transition=transition, resolve_stream_url=None, sample_rate_policy_is_auto=lambda: False,
            capture_source_intent=lambda: SimpleNamespace(source=("internal", 7)),
            commit_coordinated_track=lambda *args, **kwargs: None, set_track_context=lambda *args: None)
        queue = PlaybackQueue(deps)
        queue.tracks = [{"id": "a", "url": "a.flac"}, {"id": "b", "url": "b.flac"}]
        queue.index = 0
        with patch.object(queue.stream_prefetch, "invalidate"), \
                patch.object(queue.stream_prefetch, "reconcile_current"):
            self.assertTrue(await queue.load_track(1))
            queue.mode = "native_mpv"
            with patch.object(queue, "_reorder_native_mpv_playlist", AsyncMock(return_value=False)):
                self.assertTrue(await queue.set_shuffle(True))
        self.assertEqual([request.rate_change for request in requests], [True, True])
        self.assertEqual([request.source_generation for request in requests], [7, 7])
        self.assert_off_loop()

    async def test_source_change_during_queue_rate_read_never_starts_stale_playback(self):
        for shuffle in (False, True):
            generation = [7]
            def rate_change(rate):
                generation[0] = 8
                return True
            def ensure(captured):
                if captured.source[1] != generation[0]:
                    raise main.HTTPException(status_code=409, detail="source changed")
            transition = AsyncMock()
            deps = SimpleNamespace(
                player=lambda: SimpleNamespace(state={}), transition_is_active=lambda: False,
                coordinator_target_rate=lambda *args: 48000, coordinator_rate_change=rate_change,
                run_transition=transition, resolve_stream_url=None, sample_rate_policy_is_auto=lambda: False,
                capture_source_intent=lambda: SimpleNamespace(source=("internal", generation[0])),
                ensure_source_intent_current=ensure)
            queue = PlaybackQueue(deps)
            queue.tracks = [{"id": "a", "url": "a.flac"}, {"id": "b", "url": "b.flac"}]
            queue.index = 0
            queue.mode = "native_mpv" if shuffle else "app_replace"
            with patch.object(queue.stream_prefetch, "invalidate"), \
                    patch.object(queue.stream_prefetch, "reconcile_current"), \
                    patch.object(queue, "_reorder_native_mpv_playlist", AsyncMock(return_value=False)):
                with self.assertRaises(main.HTTPException) as caught:
                    await (queue.set_shuffle(True) if shuffle else queue.load_track(1))
            self.assertEqual(caught.exception.status_code, 409)
            transition.assert_not_awaited()

    async def test_clear_during_queue_discovery_never_replays_or_resurrects_the_queue(self):
        for shuffle in (False, True):
            entered, release = threading.Event(), threading.Event()
            def rate_change(rate):
                entered.set()
                release.wait(2)
                return True
            transition = AsyncMock(return_value=SimpleNamespace(committed=True, target_rate=48000))
            deps = SimpleNamespace(
                player=lambda: SimpleNamespace(state={}, _running=False), player_is_running=lambda: False,
                transition_is_active=lambda: False, coordinator_target_rate=lambda *args: 48000,
                coordinator_rate_change=rate_change, run_transition=transition, resolve_stream_url=None,
                sample_rate_policy_is_auto=lambda: False,
                commit_coordinated_track=lambda *args, **kwargs: None, set_track_context=lambda *args: None)
            queue = PlaybackQueue(deps)
            queue.tracks = [{"id": "a", "url": "a.flac"}, {"id": "b", "url": "b.flac"}]
            queue.index = 0
            queue.mode = "native_mpv" if shuffle else "app_replace"
            with patch.object(queue.stream_prefetch, "invalidate"), \
                    patch.object(queue.stream_prefetch, "reconcile_current"), \
                    patch.object(queue, "_reorder_native_mpv_playlist", AsyncMock(return_value=False)):
                task = asyncio.create_task(queue.set_shuffle(True) if shuffle else queue.load_track(1))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    queue.reset()
                finally:
                    release.set()
                with self.assertRaises(main.HTTPException) as caught:
                    await task
            self.assertEqual(caught.exception.status_code, 409)
            transition.assert_not_awaited()
            self.assertEqual(queue.tracks, [])
            self.assertEqual(queue.index, -1)

    async def test_rejected_navigation_does_not_invalidate_a_committing_native_shuffle(self):
        entered, release = asyncio.Event(), asyncio.Event()
        active = False
        backend = ["a", "b", "c"]
        async def transition(request):
            nonlocal active
            active = True
            entered.set()
            await release.wait()
            backend[:] = [track["id"] for track in request.native_queue]
            active = False
            return SimpleNamespace(committed=True, target_rate=48000)
        deps = SimpleNamespace(
            player=lambda: SimpleNamespace(state={}, _running=False),
            transition_is_active=lambda: active, coordinator_target_rate=lambda *args: 48000,
            coordinator_rate_change=lambda rate: False, run_transition=transition,
            set_track_context=lambda *args: None)
        queue = PlaybackQueue(deps)
        queue.tracks = [{"id": name, "url": name + ".flac"} for name in backend]
        queue.index, queue.mode = 0, "native_mpv"
        target = [queue.tracks[index] for index in (0, 2, 1)]
        with patch.object(queue.stream_prefetch, "reconcile_current"), \
                patch.object(queue, "_reorder_native_mpv_playlist", AsyncMock(return_value=False)), \
                patch("playback.queue.shuffle_around_current", return_value=target):
            shuffle = asyncio.create_task(queue.set_shuffle(True))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                with self.assertRaises(main.HTTPException) as caught:
                    await queue.load_track(1)
                self.assertEqual(caught.exception.status_code, 409)
            finally:
                release.set()
            self.assertTrue(await shuffle)
        self.assertEqual([track["id"] for track in queue.tracks], ["a", "c", "b"])
        self.assertEqual(backend, [track["id"] for track in queue.tracks])
        self.assertTrue(queue.shuffle)

    async def test_available_spl_recorder_help_probe_off_loop(self):
        from test_spl_pw_record_capture import pactl_result
        def run_help(command, **kwargs):
            self.assertEqual(command, ["pw-record", "--help"])
            self.seen.append(threading.get_ident())
            return SimpleNamespace(returncode=0, stdout="--sample-count", stderr="")
        store = MeasurementStore.__new__(MeasurementStore)
        store.audio_adapter = MeasurementAudioAdapter(command_runner=run_help)
        deps = SimpleNamespace(get_measurement_store=lambda: store, get_dsp_manager=lambda: None,
                               get_measurement_session=lambda: None,
                               read_measurement_settings=lambda: {"selectedMicInputChannel": "1"})
        with patch.object(spl, "_dependencies", return_value=deps), \
                patch.object(spl, "_spl_auto_capability", return_value={"available": True,
                    "selected_input": {"node_name": "mic"}, "microphone_model": "UMIK-1"}), \
                patch.object(spl, "_register_operation", AsyncMock()), \
                patch.object(spl.subprocess, "run", side_effect=pactl_result), \
                patch.object(spl.subprocess, "Popen", side_effect=RuntimeError("recorder boundary")):
            with self.assertRaises(main.HTTPException) as caught:
                await spl.measure_spl_automatically()
            self.assertIn("recorder boundary", caught.exception.detail)
        self.assert_off_loop()

    async def test_concurrent_calibration_starts_keep_exactly_one_visible_worker(self):
        for api, resolve_name, start_name in (
                (level_api, "_resolve_scope", "start_level_match"),
                (polarity_api, "_resolve_area", "start_polarity_check")):
            slot = JobSlot("regression")
            barrier = threading.Barrier(2)
            workers = []
            def resolve(bank_id):
                barrier.wait(2)
                return {"mode": "stereo"}, {"units": [], "slot": "global"}
            async def worker(*args):
                workers.append(asyncio.current_task())
                await asyncio.Event().wait()
            deps = SimpleNamespace(auto_sub_active=lambda: False, get_session=lambda: None,
                                   get_table=lambda: SimpleNamespace(entries=lambda mode: {}))
            with patch.object(api, "_slot", slot), patch.object(api, "_dependencies", return_value=deps), \
                    patch.object(api, resolve_name, resolve), patch.object(api, "_run", worker), \
                    patch.object(level_api, "_reference_ready", return_value=True):
                try:
                    start = getattr(api, start_name)
                    results = await asyncio.gather(
                        start(input_id="mic", bank_id="main"), start(input_id="mic", bank_id="main"),
                        return_exceptions=True)
                    await asyncio.sleep(0)
                    accepted = [result for result in results if isinstance(result, dict)]
                    self.assertEqual(len(accepted), 1)
                    rejected = [result for result in results if isinstance(result, main.HTTPException)]
                    self.assertEqual([result.status_code for result in rejected], [409])
                    self.assertEqual(len(workers), 1)
                    self.assertEqual(slot.job["id"], accepted[0]["job"]["id"])
                finally:
                    for task in workers:
                        task.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)

    async def test_level_reset_does_not_clear_a_job_started_during_discovery(self):
        slot = JobSlot("regression")
        entered, release = threading.Event(), threading.Event()
        def resolve(bank_id):
            entered.set()
            release.wait(2)
            return {"mode": "stereo", "revision": 1, "roles": []}, {"family": "main"}
        writes = []
        table = SimpleNamespace(entries=lambda mode: {}, save_entries=lambda *args: writes.append(args))
        deps = SimpleNamespace(auto_sub_active=lambda: False, get_table=lambda: table)
        with patch.object(level_api, "_slot", slot), patch.object(level_api, "_dependencies", return_value=deps), \
                patch.object(level_api, "_resolve_scope", resolve), \
                patch.object(level_api.level_match, "zero_slot_values", return_value={}):
            task = asyncio.create_task(level_api.reset_level_match(bank_id="main"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                slot.job = {"id": "newer", "status": "running"}
            finally:
                release.set()
            with self.assertRaises(main.HTTPException) as caught:
                await task
            self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(writes, [])

    async def test_spl_profile_and_capability_reads_off_loop(self):
        manager = SimpleNamespace(load_global_extras=lambda: {"loudness": {}})
        deps = SimpleNamespace(require_dsp_manager=lambda: manager, get_dsp_manager=lambda: manager,
                               get_measurement_session=lambda: None)
        with patch.object(spl, "_dependencies", return_value=deps), \
                patch.object(spl, "_spl_output_profile", self.probe({"id": "dev"})), \
                patch.object(spl, "_spl_auto_capability", self.probe({"available": False, "reason": "no mic"})):
            self.assertEqual((await spl.get_spl_calibration())["output_profile"]["id"], "dev")
            with self.assertRaises(main.HTTPException) as caught:
                await spl.measure_spl_automatically()
            self.assertEqual(caught.exception.status_code, 409)
        self.assert_off_loop()

    async def test_autosub_release_factory_off_loop(self):
        registered = []
        async def register(adapter):
            registered.append(adapter)
        marker = object()
        services = SimpleNamespace(build_autosub_release_adapter=self.probe(marker))
        with patch.object(session, "_measurement_services", return_value=services), \
                patch("measurement.autosub.jobs._measurement_session", return_value=SimpleNamespace(register_autosub_release_adapter=register)):
            await _register_autosub_release_adapter({"id": "job", "output_state_context": {
                "committed_revision": 3, "output_key": "dev", "channels": 2}})
        self.assertEqual(registered, [marker])
        self.assert_off_loop()

    async def test_release_live_device_validation_off_loop(self):
        for factory in (create_release_adapter, create_speaker_release_adapter):
            adapter = factory(
                service=None, dsp_manager=SimpleNamespace(compile_engine_text=lambda *a, **k: ""),
                get_native_runtime=lambda: None, output_key="dev", channels=2,
                hardware_ports=["a", "b"], resolve_live_device=self.probe({
                    "output_key": "other", "channels": 2, "hardware_ports": ["a", "b"]}))
            with self.assertRaises(RuntimeError):
                await adapter(48000)
        self.assert_off_loop()


class SpeakerDiscoveryTests(ServiceFixture, unittest.IsolatedAsyncioTestCase):
    async def test_close_during_discovery_invalidates_the_original_entry(self):
        epoch = 0
        entered, release = threading.Event(), threading.Event()
        def scope_factory(job_id):
            captured = epoch
            @asynccontextmanager
            async def scope():
                if captured != epoch:
                    raise RuntimeError("entry invalidated")
                yield
            return scope()
        def describe(state):
            entered.set()
            release.wait(2)
            return self.describe(state)
        service = self.service(describe=describe, job_scope=scope_factory)
        task = asyncio.create_task(service.start("left", input_id="mic", reference_input_channel="2",
                                                reference_id="reference", microphone_position_id="seat"))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            epoch += 1
        finally:
            release.set()
        job_id = await task
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "entry invalidated")
        self.assertEqual(self.acquire_calls, [])

    async def test_speaker_start_and_worker_discovery_off_loop(self):
        seen = []
        loop_thread = threading.get_ident()
        def describe(state):
            seen.append(threading.get_ident())
            return self.describe(state)
        service = self.service(describe=describe)
        result = service.start("left", input_id="mic", reference_input_channel="2",
                               reference_id="reference", microphone_position_id="seat")
        job_id = await result if inspect.isawaitable(result) else result
        job = await self.wait_terminal(service, job_id)
        self.assertEqual(job["status"], "committed")
        self.assertGreaterEqual(len(seen), 2)
        self.assertNotIn(loop_thread, seen)


if __name__ == "__main__":
    unittest.main()
