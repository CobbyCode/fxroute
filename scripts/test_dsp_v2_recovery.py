#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed v2 recovery without audio hardware or a running application."""

import asyncio
import copy
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import main
import test_output_state_lifecycle as lifecycle
import test_dsp_v2_sync_preference as sync_support
from audio.output_state import set_crossover, set_mode_routing, set_output_processing, set_bank_preset
from dsp.runtime import DSPRuntime, CommandResult
from dsp.orchestration import DspOrchestrator

PORTS = [f"playback_AUX{i}" for i in range(4)]


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.manager = lifecycle.make_manager(directory.name)
        self.service = lifecycle.make_service(directory.name, self.manager)
        state = set_crossover(self.service.load(), "stereo", True)
        state = set_mode_routing(state, "stereo", "A",
                                 ["left_high", "right_high", "left_low", "right_low"])
        filt = {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000}
        for role in ("left_high", "right_high", "left_low", "right_low"):
            state = set_output_processing(state, "stereo", role,
                                          **{"highpass" if "high" in role else "lowpass": filt})
        self.good = self.service.commit(state, expected_revision=0)
        self.engine = mock.MagicMock()
        self.engine.snapshot.return_value = {"active": False, "config": {}}
        self.engine.fail_closed = mock.AsyncMock()
        self.engine.preflight_rendered = mock.AsyncMock()
        self.context = lifecycle.lifecycle_context(self.service, self.manager, self.engine)
        self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.overview = main.get_audio_output_overview()

    def remember(self):
        self.service.remember_applied(self.good, output_key="A", channels=4,
                                      hardware_ports=PORTS, sample_rate_hz=48000)

    def draft(self):
        return self.service.commit(set_mode_routing(
            self.good, "stereo", "A", ["off", "off", "left_low", "right_low"]),
            expected_revision=1)

    async def render(self, rate=48000):
        return await main._try_render_v2_sync_target(rate, self.overview)

    async def test_unrecoverable_draft_raises_instead_of_returning_legacy_permission(self):
        self.draft()
        with self.assertRaises(RuntimeError):
            await self.render()
        self.engine.fail_closed.assert_awaited_once()

    async def test_running_same_device_v2_graph_is_not_stopped_on_render_failure(self):
        self.remember()
        self.draft()
        self.engine.snapshot.return_value = {
            "active": True, "config": {"plan_fingerprint": self.service.fingerprint(
                self.good, output_key="A", channels=4, sample_rate_hz=48000), "output_key": "A",
                                       "hardware_ports": PORTS, "sample_rate": 48000}}
        with mock.patch.object(self.service, "_deps", replace(
                self.service._deps, preset_loader=mock.Mock(side_effect=FileNotFoundError("removed")))):
            with self.assertRaises(RuntimeError):
                await self.render()
        self.engine.fail_closed.assert_not_awaited()

    async def test_measurement_staged_and_wrong_rate_graphs_are_not_production_recovery(self):
        self.remember()
        fingerprint = self.service.fingerprint(self.good, output_key="A", channels=4, sample_rate_hz=48000)
        self.draft()
        for fp, rate, measuring in (("staged", 48000, False),
                                    (fingerprint, 48000, True), (fingerprint, 44100, False)):
            self.engine.fail_closed.reset_mock()
            self.engine.snapshot.return_value = {
                "active": True, "measurement_active": measuring,
                "config": {"plan_fingerprint": fp, "sample_rate": rate,
                           "output_key": "A", "hardware_ports": PORTS}}
            with mock.patch.object(self.service, "_deps", replace(
                    self.service._deps, preset_loader=mock.Mock(side_effect=FileNotFoundError("removed")))):
                with self.assertRaises(RuntimeError):
                    await self.render()
            self.engine.fail_closed.assert_awaited_once()

    async def test_legacy_graph_is_stopped_even_when_active(self):
        self.draft()
        self.engine.snapshot.return_value = {"active": True, "config": {"output_key": "A"}}
        with self.assertRaises(RuntimeError):
            await self.render()
        self.engine.fail_closed.assert_awaited_once()

    async def test_recovery_survives_service_restart_and_recompiles_at_new_rate(self):
        self.remember()
        draft = self.draft()
        # A fresh service proves recovery is durable, not just an in-memory cache.
        service = lifecycle.make_service(str(self.directory), self.manager)
        with mock.patch.object(main, "get_output_service", return_value=service):
            target = await self.render(44100)
        self.assertEqual(target.config.sample_rate, 44100)
        self.assertEqual(target.config.route_pairs,
                         ((2, "playback_AUX0"), (4, "playback_AUX1"),
                          (1, "playback_AUX2"), (3, "playback_AUX3")))
        self.assertEqual([row["name"] for row in target.config.layout],
                         ["left_low", "left_high", "right_low", "right_high"])
        self.assertTrue(target.config.layout[1]["sos"])
        self.assertTrue(target.config.layout[3]["sos"])
        original = await main._try_render_v2_sync_target(48000, self.overview)
        self.assertNotEqual(target.config.layout[1]["sos"], original.config.layout[1]["sos"])
        self.assertEqual(service.load(), draft)
        self.engine.fail_closed.assert_not_awaited()

    async def test_missing_draft_preset_recovers_but_missing_applied_preset_fails_closed(self):
        self.remember()
        bad = set_bank_preset(self.good, "stereo", "global", preset="Missing")
        self.service.commit(bad, expected_revision=1)
        target = await self.render()
        self.assertEqual(len(target.config.layout), 4)
        # The checkpoint is not an asset archive: unavailable dependencies must fail closed.
        with mock.patch.object(self.service, "_deps", replace(
                self.service._deps, preset_loader=mock.Mock(side_effect=FileNotFoundError("removed")))):
            with self.assertRaises(RuntimeError):
                await self.render()
        self.engine.fail_closed.assert_awaited_once()

    async def test_bank_assignment_does_not_report_recovered_old_plan_as_newly_applied(self):
        self.remember()
        bad = set_bank_preset(self.good, "stereo", "global", preset="Missing")
        self.service.commit(bad, expected_revision=1)
        self.engine.sync_rendered = mock.AsyncMock()
        result = await main._sync_v2_head_after_bank_assign()
        self.assertFalse(result["live_applied"])
        self.engine.sync_rendered.assert_not_awaited()

    async def test_missing_ir_in_draft_recovers_without_ignoring_it(self):
        self.remember()
        self.manager.library("high").write("Missing IR", {
            "schema": "fxroute.dsp.preset", "version": 1,
            "chain": [{"type": "convolver", "id": "ir", "params": {"kernel": "removed.wav"}}]})
        bad = set_bank_preset(self.good, "stereo", "left_high", preset="Missing IR")
        self.service.commit(bad, expected_revision=1)
        with mock.patch.object(self.service, "_deps", replace(
                self.service._deps, resolve_ir=main._resolve_state_ir)):
            target = await self.render()
        self.assertEqual(len(target.config.layout), 4)
        self.assertTrue(target.config.layout[1]["sos"])

    async def test_checkpoint_never_crosses_device_capacity_or_port_order(self):
        self.remember()
        self.draft()
        for changes in ({"effective_output_key": "B"},
                        {"effective_output_channels": 6},
                        {"hardware_playback_ports": list(reversed(PORTS))}):
            with self.subTest(changes=changes):
                overview = copy.deepcopy(self.overview)
                overview["output_mode"].update(changes)
                with self.assertRaises(RuntimeError):
                    await main._try_render_v2_sync_target(48000, overview)

    async def test_render_alone_does_not_checkpoint_an_unapplied_state(self):
        target = await self.render()
        self.assertEqual(len(target.config.layout), 4)
        self.draft()
        with self.assertRaises(RuntimeError):
            await self.render()

    async def test_corrupt_head_recovers_without_overwriting_its_bytes(self):
        self.remember()
        path = self.directory / "output-state.json"
        path.write_text("{broken")
        target = await self.render()
        self.assertTrue(target.config.layout[1]["sos"])
        self.assertEqual(path.read_text(), "{broken")

    async def test_corrupt_checkpoint_is_not_treated_as_a_default_stereo_state(self):
        self.remember()
        self.draft()
        (self.directory / "output-state-applied.json").write_text("{}")
        with self.assertRaises(RuntimeError):
            await self.render()
        self.engine.fail_closed.assert_awaited_once()

    async def test_missing_head_recovers_instead_of_activating_unwritten_defaults(self):
        self.remember()
        (self.directory / "output-state.json").unlink()
        target = await self.render()
        self.assertEqual(len(target.config.layout), 4)
        self.assertTrue(target.config.layout[1]["sos"])

    async def test_no_persisted_state_and_no_checkpoint_stays_silent(self):
        (self.directory / "output-state.json").unlink()
        with self.assertRaises(RuntimeError):
            await self.render()
        self.engine.fail_closed.assert_awaited_once()

    async def test_valid_new_head_wins_and_only_application_promotes_it(self):
        self.remember()
        changed = set_output_processing(self.good, "stereo", "left_high", level_db=-7)
        self.service.commit(changed, expected_revision=1)
        target = await self.render()
        self.assertEqual(target.config.layout[1]["gain_db"], -7)
        self.assertEqual(self.service.load_applied(
            output_key="A", channels=4, hardware_ports=PORTS), self.good)

    async def test_native_preflight_failure_tries_checkpoint_before_any_live_sync(self):
        self.remember()
        changed = set_output_processing(self.good, "stereo", "left_high", level_db=-7)
        self.service.commit(changed, expected_revision=1)
        calls = []
        async def command(args):
            calls.append(tuple(args))
            if str(args[0]).endswith("fxroute-dsp-offline"):
                if "output 1 -7 " in Path(args[1]).read_text():
                    return CommandResult(1, "", "Required LV2 plugin is unavailable")
                return CommandResult(0)
            return CommandResult(0, "Mute: yes\n")
        binary = self.directory / "fxroute-dsp"
        binary.touch()
        native = DSPRuntime(self.manager, binary=binary, command_runner=command)
        with mock.patch.object(main.runtime, "dsp_runtime", native):
            target = await self.render()
        self.assertEqual(target.config.layout[1]["gain_db"], 0)
        self.assertEqual(len(calls), 2)
        self.assertFalse(native.snapshot()["active"])

    async def test_unrecoverable_native_preflight_failure_mutes_and_exposes_error(self):
        calls = []
        async def command(args):
            calls.append(tuple(args))
            if str(args[0]).endswith("fxroute-dsp-offline"):
                return CommandResult(1, "", "Required LV2 plugin is unavailable")
            return CommandResult(0, "Mute: yes\n")
        binary = self.directory / "fxroute-dsp"
        binary.touch()
        native = DSPRuntime(self.manager, binary=binary, command_runner=command)
        with mock.patch.object(main.runtime, "dsp_runtime", native):
            with self.assertRaises(RuntimeError):
                await self.render()
        self.assertIn(("pactl", "set-sink-mute", "A", "1"), calls)
        self.assertIn("Required LV2 plugin", native.snapshot()["last_error"])

    async def test_real_runtime_sync_restart_and_stale_repair_keep_crossover(self):
        staged = []
        async def command(args):
            if str(args[0]).endswith("fxroute-dsp-offline"):
                staged.append(Path(args[1]).read_text())
            if tuple(args[:2]) == ("pw-link", "-io"):
                return CommandResult(0, lifecycle.pw_link_io(
                    "input_1", "input_2", "output_1", "output_2", "output_3", "output_4",
                    "post_effect_FL", "post_effect_FR"))
            if tuple(args[:2]) == ("pw-link", "-l"):
                return CommandResult(0, "\n".join(
                    f"fxroute_dsp:output_{signal} -> A:{port}"
                    for signal, port in ((2, PORTS[0]), (4, PORTS[1]), (1, PORTS[2]), (3, PORTS[3]))))
            return CommandResult(0)
        async def launch(_args):
            return lifecycle.FakeProcess()
        binary = self.directory / "fxroute-dsp"
        binary.touch()
        engine = DSPRuntime(self.manager, binary=binary, command_runner=command, process_launcher=launch)
        engine._control = mock.AsyncMock(return_value="0")
        deps = sync_support._FakeDeps()
        deps.runtime = engine
        deps.try_render_v2_target = main._try_render_v2_sync_target
        orchestrator = DspOrchestrator(deps)
        with mock.patch.object(main.runtime, "dsp_runtime", engine):
            try:
                await orchestrator.sync_runtime()
                checkpoint = (self.directory / "output-state-applied.json").read_bytes()
                self.draft()
                await orchestrator.sync_runtime()
                self.assertEqual(len(engine.snapshot()["config"]["layout"]), 4)
                await engine.stop()
                await orchestrator.sync_runtime()
                self.assertTrue(engine.snapshot()["active"])
                deps.status = {"active_rate": 48000, "force_rate": 44100}
                overview = copy.deepcopy(self.overview)
                overview["output_mode"]["effective_output_rate"] = 44100
                await orchestrator.sync_runtime(overview, allow_unsettled_rate=True)
                snapshot = engine.snapshot()
                self.assertEqual(snapshot["config"]["sample_rate"], 44100)
                self.assertEqual(snapshot["config"]["output_routes"],
                                 [(2, PORTS[0]), (4, PORTS[1]), (1, PORTS[2]), (3, PORTS[3])])
                self.assertTrue(snapshot["config"]["layout"][1]["sos"])
                saved = json.loads((self.directory / "output-state-applied.json").read_text())
                self.assertEqual(saved["state"], json.loads(checkpoint)["state"])
                self.assertEqual(saved["sample_rate_hz"], 44100)
                self.assertTrue(all("outputs 4\n" in text for text in staged))
            finally:
                await engine.stop()

    async def test_loudness_and_temporary_effects_preserve_recovered_crossover(self):
        self.remember()
        self.draft()
        previous = self.manager.load_global_extras()
        candidate = copy.deepcopy(previous)
        candidate["loudness"]["enabled"] = True
        new, old = await main._render_effects_transition_targets(previous, candidate)
        self.assertEqual(new.config.route_pairs, old.config.route_pairs)
        self.assertIn("loud_comp", new.text)
        self.assertNotIn("loud_comp", old.text)
        self.assertTrue(new.config.layout[1]["sos"])
        with mock.patch.object(main, "dsp_manager", self.manager), \
             mock.patch.object(main, "_volume_state_for_manager", mock.AsyncMock(return_value=SimpleNamespace(
                 loudness_in_path=False, preset="Neutral", dsp_guard_db=0.0, master_percent=27))):
            async def rebuild(*args, apply_candidate, **kwargs):
                apply_candidate()
            self.engine.guarded_rebuild_rendered = mock.AsyncMock(side_effect=rebuild)
            await main._guarded_effects_transition(previous, candidate, False)
            await main._temporary_effects_transition(previous, candidate)
        self.engine.guarded_rebuild.assert_not_called()
        self.assertEqual(self.engine.guarded_rebuild_rendered.await_count, 2)


class FailClosedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_mutes_hardware_removes_helper_and_reports_error(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = []
            async def run(command):
                commands.append(tuple(command))
                return CommandResult(0, "Mute: yes\n")
            engine = DSPRuntime(lifecycle.make_manager(directory), command_runner=run)
            process = lifecycle.FakeProcess()
            engine._process = process
            await engine.fail_closed("No recoverable v2 output state", output_key="A")
            self.assertIn(("pactl", "set-sink-mute", "A", "1"), commands)
            self.assertEqual(process.returncode, 0)
            self.assertFalse(engine.snapshot()["active"])
            self.assertIn("No recoverable v2", engine.snapshot()["last_error"])


if __name__ == "__main__":
    unittest.main()
