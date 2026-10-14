#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Event-gated regressions for competing v2 graph writers."""

import asyncio
import copy
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import main
import test_output_state_lifecycle as lifecycle
import test_dsp_v2_sync_preference as sync_support
from audio.output_state import set_bank_preset, set_output_processing
from audio.output_state_store import StateConflictError
from dsp import api as dsp_api
from dsp.orchestration import DspOrchestrator
from dsp.runtime import CommandResult, DSPRuntime
from playback.runtime import FxrouteTransitionRuntime
from playback.transition import TransitionRequest


class GraphWriteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.manager = lifecycle.make_manager(directory.name)
        self.manager.LOUDNESS_STRENGTH_VOLUME_SETTLE_SECONDS = 0
        self.service = lifecycle.make_service(directory.name, self.manager)
        lifecycle.seed_sub_state(self.service)
        self.preflight_gate = None
        self.preflight_skip = 0
        self.preflight_entered = asyncio.Event()
        self.output_prepared = asyncio.Event()
        self.live_rate = 48000

        async def command(args):
            if str(args[0]).endswith("fxroute-dsp-offline"):
                if self.preflight_skip:
                    self.preflight_skip -= 1
                elif self.preflight_gate is not None:
                    gate, self.preflight_gate = self.preflight_gate, None
                    self.preflight_entered.set()
                    await gate.wait()
                return CommandResult(0)
            if tuple(args[:2]) == ("pw-link", "-io"):
                return CommandResult(0, lifecycle.pw_link_io(
                    "input_1", "input_2", "output_1", "output_2", "output_3",
                    "post_effect_FL", "post_effect_FR"))
            return CommandResult(0)

        async def launch(_args):
            return lifecycle.FakeProcess()

        binary = self.directory / "fxroute-dsp"
        binary.touch()
        self.engine = DSPRuntime(self.manager, binary=binary, command_runner=command,
                                 process_launcher=launch)
        self.engine._control = mock.AsyncMock(return_value="0")
        self.addAsyncCleanup(self.engine.stop)
        context = lifecycle.lifecycle_context(self.service, self.manager, self.engine)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.patches = mock.patch.multiple(
            main, dsp_manager=self.manager,
            _volume_state_for_manager=mock.AsyncMock(return_value=SimpleNamespace(
                loudness_in_path=True, preset="Neutral", dsp_guard_db=0.0, master_percent=27)))
        self.patches.start()
        self.addCleanup(self.patches.stop)
        for name in ("dsp_mutation_lock", "canonical_volume_write_lock", "dsp_graph_write_lock"):
            if hasattr(main.runtime, name):
                setattr(main.runtime, name, None)

        def rate_status():
            task = asyncio.current_task() if threading.current_thread() is threading.main_thread() else None
            if task is not None and task.get_name() == "output-edit":
                self.output_prepared.set()
            return {"active_rate": self.live_rate, "force_rate": self.live_rate}

        patch = mock.patch.object(main, "get_samplerate_status", side_effect=rate_status)
        patch.start()
        self.addCleanup(patch.stop)

    async def edit(self, level=-7):
        return await main.apply_audio_output_state(lifecycle.FakeRequest({
            "expected_revision": self.service.load()["revision"],
            "mutation": {"kind": "set_processing", "mode": "stereo-sub",
                         "role": "sub1", "level_db": level}}))

    def arm_gate(self, *, skip=0):
        gate = asyncio.Event()
        self.preflight_gate = gate
        self.preflight_skip = skip
        self.preflight_entered.clear()
        return gate

    async def settle_tasks(self, first, second, gate):
        try:
            await asyncio.wait_for(self.output_prepared.wait(), 2)
            # Either the newer edit finishes, or it waits for graph ownership.
            await asyncio.wait({second}, timeout=0.1)
        finally:
            gate.set()
        return await asyncio.wait_for(asyncio.gather(first, second), 5)

    async def test_loudness_commit_cannot_be_overwritten_by_old_output_extras(self):
        previous = self.manager.load_global_extras()
        candidate = copy.deepcopy(previous)
        candidate["loudness"]["enabled"] = True
        candidate["loudness"]["params"].update(strength=7, volumeDb=-40)
        gate = self.arm_gate(skip=2)
        effects = asyncio.create_task(main._guarded_effects_transition(previous, candidate, True))
        await asyncio.wait_for(self.preflight_entered.wait(), 2)
        output = asyncio.create_task(self.edit(), name="output-edit")
        await self.settle_tasks(effects, output, gate)
        self.assertEqual(self.manager.load_global_extras(), candidate)
        self.assertIn("control volume -30\n", self.engine._config_text)
        self.assertIn("param output_gain_db 30\n", self.engine._config_text)
        self.assertEqual(self.engine.snapshot()["config"]["layout"][2]["gain_db"], -7)
        self.assertEqual(self.engine.snapshot()["config"]["plan_fingerprint"], self.service.fingerprint(
            self.service.load(), output_key="A", channels=4, sample_rate_hz=48000))

    async def test_background_render_cannot_activate_after_a_newer_output_edit(self):
        deps = sync_support._FakeDeps()
        deps.runtime = self.engine
        deps.try_render_v2_target = main._try_render_v2_sync_target
        if hasattr(main, "_dsp_graph_write_lock"):
            deps.graph_write_lock = main._dsp_graph_write_lock
        gate = self.arm_gate()
        background = asyncio.create_task(DspOrchestrator(deps).sync_runtime())
        await asyncio.wait_for(self.preflight_entered.wait(), 2)
        output = asyncio.create_task(self.edit(), name="output-edit")
        await self.settle_tasks(background, output, gate)
        self.assertEqual(self.engine.snapshot()["config"]["layout"][2]["gain_db"], -7)
        self.assertEqual(self.service.load_applied(
            output_key="A", channels=4, hardware_ports=lifecycle.PORTS), self.service.load())

    async def test_head_changed_during_render_is_a_conflict_not_recovery(self):
        gate = self.arm_gate()
        render = asyncio.create_task(main._try_render_v2_sync_target(48000, main.get_audio_output_overview()))
        await asyncio.wait_for(self.preflight_entered.wait(), 2)
        self.service.apply(lambda state: set_output_processing(state, "stereo-sub", "sub1", level_db=-7),
                           expected_revision=1)
        gate.set()
        with self.assertRaises(StateConflictError):
            await render
        self.assertFalse(self.engine.snapshot()["active"])

    async def test_prebuilt_target_rechecks_freshness_after_native_preflight(self):
        target = await main._try_render_v2_sync_target(48000, main.get_audio_output_overview())
        gate = self.arm_gate()
        sync = asyncio.create_task(self.engine.sync_rendered(target))
        await asyncio.wait_for(self.preflight_entered.wait(), 2)
        self.service.apply(lambda state: set_output_processing(state, "stereo-sub", "sub1", level_db=-7),
                           expected_revision=1)
        gate.set()
        with self.assertRaises(StateConflictError):
            await sync
        self.assertFalse(self.engine.snapshot()["active"])

    async def test_extras_use_v2_global_slot_not_legacy_direct(self):
        self.manager.state_store.write("active.json", {"preset": "Direct"})
        self.manager.runtime_transition_callback = None
        with mock.patch.object(main.manager, "broadcast", mock.AsyncMock()), mock.patch.object(
                main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"):
            result = await dsp_api.save_dsp_extras(lifecycle.FakeRequest({
                "headroomEnabled": True, "headroomGainDb": -6}))
        self.assertEqual(result["status"], "ok")
        self.assertTrue(self.engine.snapshot()["active"])
        self.assertIn("param gain_db -6\n", self.engine._config_text)
        self.assertEqual(self.manager.get_active_preset(), "Direct")
        self.assertEqual(self.service.load()["modes"]["stereo-sub"]["banks"]["global"]["preset"], "Neutral")

    async def test_v2_direct_does_not_reload_legacy_preset(self):
        self.service.apply(lambda state: set_bank_preset(state, "stereo-sub", "global", preset="Direct"),
                           expected_revision=1)
        with mock.patch.object(main, "_load_dsp_preset", mock.AsyncMock()) as legacy, mock.patch.object(
                main.manager, "broadcast", mock.AsyncMock()), mock.patch.object(
                main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"):
            await dsp_api.save_dsp_extras(lifecycle.FakeRequest({"headroomGainDb": -6}))
        legacy.assert_not_awaited()
        self.assertTrue(self.engine.snapshot()["active"])
        self.assertNotIn("param gain_db -6\n", self.engine._config_text)

    def transition_request(self):
        state = self.service.load()
        candidate = set_output_processing(state, "stereo-sub", "sub1", level_db=-7)
        target = main._build_plan_target(
            self.service, self.manager, lifecycle.plan_for(self.service, candidate),
            output_key="A", rate=48000, hardware_ports=lifecycle.PORTS,
            fingerprint=self.service.fingerprint(candidate, output_key="A", channels=4, sample_rate_hz=48000))
        return TransitionRequest(
            operation="output-mode-switch", source="local", target_rate=48000,
            output_mode_target=main.get_audio_output_overview(),
            output_state_transition={"candidate_state": candidate, "previous_state": state,
                                     "expected_revision": state["revision"], "target": target,
                                     "previous_target": target, "output_key": "A", "channels": 4,
                                     "fingerprint": target.config.plan_fingerprint})

    async def test_coordinator_refreshes_extras_and_owns_graph_through_commit(self):
        request = self.transition_request()
        extras = self.manager.load_global_extras()
        extras["headroom"]["enabled"] = True
        extras["headroom"]["params"]["gainDb"] = -6
        self.manager.save_global_extras(extras)
        adapter = FxrouteTransitionRuntime(main.make_playback_runtime_deps())
        adapter._deps = replace(adapter._deps, playback_graph_diagnosis=mock.AsyncMock(
            return_value={"links_complete": True}))
        with mock.patch.object(main, "measurement_sr_session", SimpleNamespace(lock=asyncio.Lock())):
            async with adapter.transition_configuration(request):
                target = request.output_state_transition["target"]
                self.assertIn("param gain_db -6\n", target.text)
                self.assertTrue(main._dsp_graph_write_lock().locked())
                await self.engine.sync_rendered(target)
                await adapter.commit_output_mode_runtime(request)
                await adapter.finalize_output_mode_graph_after_gate_open(request)
        self.assertEqual(self.service.load_applied(
            output_key="A", channels=4, hardware_ports=lifecycle.PORTS), self.service.load())
        self.assertFalse(main._dsp_graph_write_lock().locked())

    async def test_coordinator_rejects_changed_revision_before_staging(self):
        request = self.transition_request()
        self.service.apply(lambda state: set_output_processing(state, "stereo-sub", "sub1", level_db=-3),
                           expected_revision=1)
        adapter = FxrouteTransitionRuntime(main.make_playback_runtime_deps())
        with mock.patch.object(main, "measurement_sr_session", SimpleNamespace(lock=asyncio.Lock())):
            with self.assertRaises(StateConflictError):
                async with adapter.transition_configuration(request):
                    pass
        self.assertFalse(main._dsp_graph_write_lock().locked())

    async def test_prebuilt_target_rejects_changed_extras(self):
        target = await main._try_render_v2_sync_target(48000, main.get_audio_output_overview())
        extras = self.manager.load_global_extras()
        extras["headroom"]["params"]["gainDb"] = -6
        self.manager.save_global_extras(extras)
        with self.assertRaises(StateConflictError):
            await self.engine.sync_rendered(target)
        self.assertFalse(self.engine.snapshot()["active"])

    async def test_cancelled_effects_writer_releases_graph_ownership(self):
        previous = self.manager.load_global_extras()
        gate = self.arm_gate()
        effects = asyncio.create_task(main._guarded_effects_transition(previous, previous, True))
        await asyncio.wait_for(self.preflight_entered.wait(), 2)
        effects.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await effects
        gate.set()
        self.assertFalse(main._dsp_graph_write_lock().locked())
        await asyncio.wait_for(self.edit(), 2)
        self.assertEqual(self.engine.snapshot()["config"]["layout"][2]["gain_db"], -7)

    async def test_ordinary_extras_use_guarded_worker_bridge_without_legacy_reload(self):
        loop = asyncio.get_running_loop()

        def transition(previous, candidate, persist_all):
            return asyncio.run_coroutine_threadsafe(
                main._guarded_effects_transition(previous, candidate, persist_all), loop).result(5)

        self.manager.runtime_transition_callback = transition
        self.manager.state_store.write("active.json", {"preset": "Direct"})
        with mock.patch.object(main, "_load_dsp_preset", mock.AsyncMock()) as legacy, mock.patch.object(
                main.manager, "broadcast", mock.AsyncMock()), mock.patch.object(
                main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"):
            await asyncio.wait_for(dsp_api.save_dsp_extras(lifecycle.FakeRequest({
                "headroomEnabled": True, "headroomGainDb": -6})), 5)
        legacy.assert_not_awaited()
        self.assertIn("param gain_db -6\n", self.engine._config_text)
        self.assertTrue(self.manager.load_global_extras()["headroom"]["enabled"])
        self.assertFalse(main._dsp_graph_write_lock().locked())

    async def test_fast_path_rollback_finishes_before_the_next_effects_writer(self):
        old_preflight = self.engine._preflight_text
        entered = asyncio.Event()
        release = asyncio.Event()

        async def reject_candidate(text):
            if "output 2 -7 " in text:
                entered.set()
                await release.wait()
                raise RuntimeError("candidate rejected")
            return await old_preflight(text)

        previous = self.manager.load_global_extras()
        candidate = copy.deepcopy(previous)
        candidate["headroom"]["enabled"] = True
        candidate["headroom"]["params"]["gainDb"] = -6
        with mock.patch.object(self.engine, "_preflight_text", side_effect=reject_candidate):
            output = asyncio.create_task(self.edit(), name="output-edit")
            await asyncio.wait_for(entered.wait(), 2)
            effects = asyncio.create_task(main._guarded_effects_transition(previous, candidate, True))
            try:
                done, _ = await asyncio.wait({effects}, timeout=0.1)
                self.assertFalse(done, "Effects must wait until the output rollback finishes")
            finally:
                release.set()
            with self.assertRaises(main.HTTPException):
                await asyncio.wait_for(output, 5)
            await asyncio.wait_for(effects, 5)
        self.assertEqual(self.service.load()["modes"]["stereo-sub"]["processing"]["sub1"]["level_db"], 0)
        self.assertEqual(self.engine.snapshot()["config"]["layout"][2]["gain_db"], 0)
        self.assertIn("param gain_db -6\n", self.engine._config_text)

    async def test_coordinator_stale_helper_recovery_does_not_reenter_graph_lock(self):
        target = await main._try_render_v2_sync_target(48000, main.get_audio_output_overview())
        await self.engine.sync_rendered(target)
        request = replace(self.transition_request(), target_rate=44100)
        deps = sync_support._FakeDeps()
        deps.runtime = self.engine
        deps.status = {"active_rate": 48000, "force_rate": 44100}
        deps.try_render_v2_target = main._try_render_v2_sync_target
        deps.graph_write_lock = main._dsp_graph_write_lock
        deps.wait_for_samplerate_alignment = mock.AsyncMock(return_value=True)
        orchestration = DspOrchestrator(deps)
        adapter = FxrouteTransitionRuntime(main.make_playback_runtime_deps())
        adapter._deps = replace(
            adapter._deps, get_samplerate_status=lambda: deps.status,
            ensure_playback_samplerate_force=mock.AsyncMock(return_value=False),
            trigger_idle_sink_renegotiation=mock.AsyncMock(return_value=False),
            recover_stale_samplerate_helper=orchestration.recover_stale_helper_samplerate)
        with mock.patch.object(main, "measurement_sr_session", SimpleNamespace(lock=asyncio.Lock())):
            async with adapter.transition_configuration(request):
                await asyncio.wait_for(adapter.establish_target_rate(request), 1)
        self.assertEqual(self.engine.snapshot()["config"]["sample_rate"], 44100)

    async def test_queued_fast_edit_rejects_changed_rate_before_commit(self):
        async with main._dsp_graph_write_lock():
            output = asyncio.create_task(self.edit(), name="output-edit")
            await asyncio.wait_for(self.output_prepared.wait(), 2)
            self.live_rate = 96000
            target = await main._try_render_v2_sync_target(96000, main.get_audio_output_overview())
            await self.engine.sync_rendered(target)
        with self.assertRaises(main.HTTPException) as failure:
            await asyncio.wait_for(output, 5)
        self.assertEqual(failure.exception.status_code, 409)
        self.assertEqual(self.service.load()["revision"], 1)
        self.assertEqual(self.engine.snapshot()["config"]["sample_rate"], 96000)

    async def test_failed_native_extras_rollback_stops_uncertain_candidate(self):
        initial = await main._try_render_v2_sync_target(48000, main.get_audio_output_overview())
        await self.engine.sync_rendered(initial)
        previous = self.manager.load_global_extras()
        loop = asyncio.get_running_loop()
        self.manager.runtime_transition_callback = lambda old, new, persist: asyncio.run_coroutine_threadsafe(
            main._guarded_effects_transition(old, new, persist), loop).result(5)
        preflight = self.engine._preflight_text

        async def reject_rollback(text):
            if "param gain_db -6\n" in (self.engine._config_text or "") and "param gain_db -6\n" not in text:
                raise RuntimeError("rollback preflight failed")
            return await preflight(text)

        with mock.patch.object(self.engine, "_preflight_text", side_effect=reject_rollback), mock.patch.object(
                self.engine, "ramp_output_gain_db", mock.AsyncMock(side_effect=RuntimeError("ramp failed"))), mock.patch.object(
                main.preset_loading, "_restore_volume_state", mock.AsyncMock()):
            with self.assertRaises(RuntimeError):
                await dsp_api.save_dsp_extras(lifecycle.FakeRequest({
                    "headroomEnabled": True, "headroomGainDb": -6}))
        self.assertEqual(self.manager.load_global_extras(), previous)
        self.assertFalse(self.engine.snapshot()["active"])

    async def test_fast_target_rejects_newer_head_without_replaying_old_rollback(self):
        gate = self.arm_gate()
        output = asyncio.create_task(self.edit(), name="output-edit")
        await asyncio.wait_for(self.preflight_entered.wait(), 2)
        self.service.apply(lambda state: set_output_processing(state, "stereo-sub", "sub1", level_db=-3),
                           expected_revision=2)
        gate.set()
        with self.assertRaises(main.HTTPException):
            await output
        self.assertEqual(self.service.load()["modes"]["stereo-sub"]["processing"]["sub1"]["level_db"], -3)
        self.assertFalse(self.engine.snapshot()["active"])

    async def test_fast_token_cannot_accept_a_head_newer_than_its_own_commit(self):
        commit = self.service.commit_unowned

        def commit_then_race(candidate, *, expected_revision):
            committed = commit(candidate, expected_revision=expected_revision)
            self.service.commit(set_output_processing(
                committed, "stereo-sub", "sub1", level_db=-3), expected_revision=committed["revision"])
            return committed

        with mock.patch.object(self.service, "commit_unowned", side_effect=commit_then_race):
            with self.assertRaises(main.HTTPException):
                await self.edit()
        self.assertEqual(self.service.load()["revision"], 3)
        self.assertFalse(self.engine.snapshot()["active"])

    async def commit_newer_external_graph(self, expected_revision):
        newer = self.service.apply(lambda state: set_output_processing(
            state, "stereo-sub", "sub1", level_db=-3), expected_revision=expected_revision)
        target = main._build_plan_target(
            self.service, self.manager, lifecycle.plan_for(self.service, newer),
            output_key="A", rate=48000, hardware_ports=lifecycle.PORTS,
            fingerprint=self.service.fingerprint(newer, output_key="A", channels=4, sample_rate_hz=48000))
        # Model an external owner changing the native graph outside the app locks.
        self.engine._config = target.config
        self.engine._config_text = target.text

    async def test_post_revert_fast_rollback_rechecks_head_after_preflight(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        preflight = self.engine._preflight_text
        old_preflights = 0

        async def reject_then_pause_rollback(text):
            nonlocal old_preflights
            if "output 2 -7 " in text:
                raise RuntimeError("candidate rejected")
            old_preflights += 1
            if old_preflights == 2:
                entered.set()
                await release.wait()
            return await preflight(text)

        with mock.patch.object(self.engine, "_preflight_text", side_effect=reject_then_pause_rollback):
            output = asyncio.create_task(self.edit(), name="output-edit")
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await self.commit_newer_external_graph(3)
            finally:
                release.set()
            with self.assertRaises(main.HTTPException):
                await output
        self.assertEqual(self.service.load()["revision"], 4)
        self.assertEqual(self.engine.snapshot()["config"]["layout"][2]["gain_db"], -3)

    async def test_coordinator_rollback_rechecks_reverted_head_after_preflight(self):
        request = self.transition_request()
        main._refresh_output_state_transition(request)
        await self.engine.sync_rendered(request.output_state_transition["target"])
        self.service.commit(request.output_state_transition["candidate_state"], expected_revision=1)
        adapter = FxrouteTransitionRuntime(main.make_playback_runtime_deps())
        adapter._verify_plan_rollback = mock.AsyncMock()
        gate = self.arm_gate()
        rollback = asyncio.create_task(adapter.rollback_output_mode_runtime(request, {}))
        try:
            await asyncio.wait_for(self.preflight_entered.wait(), 2)
            await self.commit_newer_external_graph(3)
        finally:
            gate.set()
        with self.assertRaises(StateConflictError):
            await rollback
        self.assertEqual(self.service.load()["revision"], 4)
        self.assertEqual(self.engine.snapshot()["config"]["layout"][2]["gain_db"], -3)

    async def test_internal_fast_rollback_rechecks_head_after_preflight(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        preflight = self.engine._preflight_text

        async def reject_then_pause_rollback(text):
            if "output 2 -7 " in text:
                raise RuntimeError("candidate rejected")
            entered.set()
            await release.wait()
            return await preflight(text)

        with mock.patch.object(self.engine, "_preflight_text", side_effect=reject_then_pause_rollback):
            output = asyncio.create_task(self.edit(), name="output-edit")
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await self.commit_newer_external_graph(2)
            finally:
                release.set()
            with self.assertRaises(main.HTTPException):
                await output
        self.assertEqual(self.service.load()["revision"], 3)
        self.assertEqual(self.engine.snapshot()["config"]["layout"][2]["gain_db"], -3)


if __name__ == "__main__":
    unittest.main()
