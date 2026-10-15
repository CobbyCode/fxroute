#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Effective extras identity and external measurement graph ownership."""

import asyncio
import copy
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import main
from dsp.output_render import build_plan_target
import test_output_state_lifecycle as lifecycle
from audio.output_state import set_bank_preset
from dsp import api as dsp_api
from measurement import session as measurement_session
from measurement.target import targets_compatible


class MeasurementConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.manager = lifecycle.make_manager(directory.name)
        factory = mock.patch.multiple(
            main, _output_service_instance=None,
            _output_state_store_path=mock.Mock(return_value=self.directory / "output-state.json"),
            _require_dsp_manager=mock.Mock(return_value=self.manager))
        factory.start()
        self.addCleanup(factory.stop)
        self.service = main.get_output_service()
        self.state = lifecycle.seed_sub_state(self.service)
        self.manager.active_preset_reader = main._active_global_preset
        self.native = SimpleNamespace(
            preflight_rendered=mock.AsyncMock(), fail_closed=mock.AsyncMock(),
            snapshot=lambda: {"active": False}, sync_rendered=mock.AsyncMock())
        context = lifecycle.lifecycle_context(self.service, self.manager, self.native)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.owner = SimpleNamespace(has_active_jobs=False, entry_in_progress=False,
                                     active=True, lock=asyncio.Lock())
        patches = mock.patch.multiple(
            main, measurement_sr_session=self.owner, dsp_manager=self.manager,
            _volume_state_for_manager=mock.AsyncMock())
        patches.start()
        self.addCleanup(patches.stop)
        for patch in (mock.patch.object(main.manager, "broadcast", mock.AsyncMock()),
                      mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change")):
            patch.start()
            self.addCleanup(patch.stop)
        for name in ("dsp_mutation_lock", "canonical_volume_write_lock", "dsp_graph_write_lock"):
            setattr(main.runtime, name, None)

    def plan(self, *, alignment=False):
        compile_plan = self.service.compile_alignment_plan if alignment else self.service.compile_plan
        return compile_plan(self.service.load(), output_key="A", channels=4, sample_rate_hz=48000)

    def render(self, plan):
        return build_plan_target(
            self.service, self.manager, plan, output_key="A", rate=48000,
            hardware_ports=lifecycle.PORTS, fingerprint=self.service.fingerprint_plan(plan))

    async def test_extras_edits_between_jobs_make_targets_incompatible(self):
        initial = self.manager.load_global_extras()
        for section, update in (
                ("loudness", {"enabled": True, "params": {"strength": 7}}),
                ("bass_enhancer", {"enabled": True, "params": {"amount": 0.4}}),
                ("tone_effect", {"enabled": True, "mode": "crystalizer"})):
            with self.subTest(section=section):
                self.manager.save_global_extras(initial)
                before = main._freeze_measurement_target("global", 48000)
                old_text = self.render(self.plan()).text
                candidate = copy.deepcopy(initial)
                candidate[section].update(update)
                self.manager.save_global_extras(candidate)
                after = main._freeze_measurement_target("global", 48000)
                self.assertNotEqual(old_text, self.render(self.plan()).text)
                self.assertEqual(before["revision"], after["revision"])
                self.assertFalse(targets_compatible(before, after))

    async def test_renderer_keeps_the_fingerprinted_extras_snapshot(self):
        extras = self.manager.load_global_extras()
        extras["headroom"].update(enabled=True, params={"gainDb": -6})
        self.manager.save_global_extras(extras)
        plan = self.plan()
        expected = self.render(plan)
        extras["headroom"]["params"]["gainDb"] = -3
        self.manager.save_global_extras(extras)
        rendered = self.render(plan)
        self.assertEqual(rendered.text, expected.text)
        self.assertIn("param gain_db -6\n", rendered.text)
        self.assertEqual(rendered.config.plan_fingerprint, expected.config.plan_fingerprint)

    async def test_transition_and_rollback_checkpoint_have_their_own_identity(self):
        previous = self.manager.load_global_extras()
        candidate = copy.deepcopy(previous)
        candidate["headroom"].update(enabled=True, params={"gainDb": -6})
        new, old = await main.output_renderer.render_effects_targets(previous, candidate)
        self.assertNotEqual(new.config.plan_fingerprint, old.config.plan_fingerprint)
        for target in (new, old):
            target.on_applied()
            record = self.service._applied_record(
                output_key="A", channels=4, hardware_ports=lifecycle.PORTS)
            self.assertEqual(record["fingerprint"], target.config.plan_fingerprint)

    async def test_direct_and_alignment_bypass_ignore_global_extras(self):
        normal = self.render(self.plan(alignment=True))
        self.service.apply(lambda state: set_bank_preset(
            state, "stereo-sub", "global", preset="Direct"), expected_revision=1)
        direct = self.render(self.plan())
        self.manager.save_global_extras({"loudness": {"enabled": True},
                                        "bass_enhancer": {"enabled": True},
                                        "tone_effect": {"enabled": True}})
        for before, plan in ((normal, self.plan(alignment=True)), (direct, self.plan())):
            after = self.render(plan)
            self.assertEqual(before.config.plan_fingerprint, after.config.plan_fingerprint)
            self.assertEqual(before.text, after.text)

    async def test_active_job_rejects_extras_without_disk_or_runtime_change(self):
        self.owner.has_active_jobs = True
        previous = self.manager.load_global_extras()
        with self.assertRaises(main.HTTPException) as failure:
            await dsp_api.save_dsp_extras(lifecycle.FakeRequest({"headroomGainDb": -6}))
        self.assertEqual(failure.exception.status_code, 423)
        self.assertEqual(self.manager.load_global_extras(), previous)
        self.native.sync_rendered.assert_not_awaited()

    async def test_unchanged_extras_remain_allowed_during_a_job(self):
        self.owner.has_active_jobs = True
        result = await dsp_api.save_dsp_extras(lifecycle.FakeRequest({}))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["updated_presets"], 0)
        self.native.sync_rendered.assert_not_awaited()

    async def test_idle_measurement_window_allows_extras(self):
        result = await dsp_api.save_dsp_extras(lifecycle.FakeRequest({"headroomGainDb": -6}))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.manager.load_global_extras()["headroom"]["params"]["gainDb"], -6)

    async def test_bank_activation_during_job_does_not_change_active_preset(self):
        self.owner.has_active_jobs = True
        with self.assertRaises(main.HTTPException) as failure:
            await main.apply_audio_output_state(lifecycle.FakeRequest({
                "expected_revision": 1,
                "mutation": {"kind": "set_bank_preset", "mode": "stereo-sub",
                             "bank_id": "global", "preset": "Room"}}))
        self.assertEqual(failure.exception.status_code, 423)
        self.assertEqual(self.manager.get_active_preset(), "Neutral")

    async def test_queued_extras_request_holds_session_while_waiting_for_mutation(self):
        lock = main._dsp_mutation_lock()
        mutation_attempted = asyncio.Event()
        original_acquire = lock.acquire

        async def acquire_and_signal():
            mutation_attempted.set()
            return await original_acquire()

        async with lock:
            with mock.patch.object(lock, "acquire", side_effect=acquire_and_signal):
                task = asyncio.create_task(dsp_api.save_dsp_extras(
                    lifecycle.FakeRequest({"bassEnabled": True})))
                # Writer reserves the session first, then waits for the
                # mutation lock without holding inner locks while waiting.
                await asyncio.wait_for(mutation_attempted.wait(), 1)
                self.assertTrue(self.owner.lock.locked())
                registration = asyncio.create_task(
                    self.owner.lock.acquire())
                try:
                    done, _ = await asyncio.wait({registration}, timeout=0.05)
                    self.assertFalse(done)
                finally:
                    pass
        await asyncio.wait_for(task, 1)
        registration.cancel()
        try:
            await registration
        except asyncio.CancelledError:
            pass
        self.assertTrue(self.manager.load_global_extras()["bass_enhancer"]["enabled"])
        self.native.sync_rendered.assert_awaited_once()

    async def test_extras_reserve_graph_until_runtime_reconciliation_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        registration_entered = asyncio.Event()

        @asynccontextmanager
        async def entry_guard():
            registration_entered.set()
            yield

        async def reconcile(_target):
            entered.set()
            await release.wait()

        self.native.sync_rendered.side_effect = reconcile
        session = measurement_session.MeasurementSampleRateSession()
        session.active = True
        with mock.patch.object(main, "measurement_sr_session", session), mock.patch.object(
                measurement_session, "_services", SimpleNamespace(input_entry_guard=entry_guard)):
            writer = asyncio.create_task(dsp_api.save_dsp_extras(
                lifecycle.FakeRequest({"toneEffectEnabled": True})))
            await asyncio.wait_for(entered.wait(), 1)
            registration = asyncio.create_task(session.register_manual_job("next-sweep"))
            try:
                await asyncio.wait_for(registration_entered.wait(), 1)
                self.assertFalse(registration.done())
                self.assertFalse(session.has_active_jobs)
            finally:
                release.set()
                await asyncio.wait_for(writer, 1)
                await asyncio.wait_for(registration, 1)
        self.assertTrue(session.has_active_jobs)
        self.assertFalse(session.lock.locked())

    async def test_inflight_measurement_entry_refuses_extras_without_waiting(self):
        async with self.owner.lock:
            with self.assertRaises(main.HTTPException) as failure:
                await asyncio.wait_for(dsp_api.save_dsp_extras(
                    lifecycle.FakeRequest({"bassEnabled": True})), 1)
        self.assertEqual(failure.exception.status_code, 423)
        self.native.sync_rendered.assert_not_awaited()

    async def test_active_preset_delete_is_refused_before_removing_the_file(self):
        self.service.apply(lambda state: set_bank_preset(
            state, "stereo-sub", "global", preset="Room"), expected_revision=1)
        self.owner.has_active_jobs = True
        with self.assertRaises(main.HTTPException) as failure:
            await dsp_api.delete_dsp_preset(lifecycle.FakeRequest({
                "preset_name": "Room", "bank_mode": "stereo-sub", "bank_id": "global"}))
        self.assertEqual(failure.exception.status_code, 423)
        self.assertEqual(self.manager.get_active_preset(), "Room")
        self.assertEqual(self.manager.library("global", "stereo-sub").read("Room")["chain"], [])

    async def test_library_only_create_and_delete_remain_allowed_during_jobs(self):
        self.owner.has_active_jobs = True
        result = await dsp_api.combine_dsp_presets(lifecycle.FakeRequest({
            "presetName": "LibraryOnly", "presetNames": ["Neutral", "Direct"]}))
        self.assertEqual(result["active_preset"], "Neutral")
        self.assertEqual(self.manager.library().read("LibraryOnly")["chain"], [])
        result = await dsp_api.delete_dsp_preset(lifecycle.FakeRequest({"preset_name": "LibraryOnly"}))
        self.assertEqual(result["deleted"], "LibraryOnly")
        self.assertEqual(self.manager.get_active_preset(), "Neutral")
        self.native.sync_rendered.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
