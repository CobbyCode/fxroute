#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""v2 live lifecycle: plan configs, rendered sync, guarded rebuild, apply fast path."""

import asyncio
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from audio.output_state import switch_mode, set_bank_preset, set_mode_routing, set_output_processing
from audio.output_state_store import OutputStateStore
from dsp.manager import DSPManager
from dsp.runtime import CommandResult, DSPRuntime, DSPRuntimeConfig, PlannedSyncTarget


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.pid = 123
        self.stderr = None

    def terminate(self):
        self.returncode = 0

    async def wait(self):
        return self.returncode


def make_manager(directory):
    manager = DSPManager(home=Path(directory) / "home")
    manager.preset_store.write("Room", {"schema": "fxroute.dsp.preset", "version": 1, "chain": []})
    manager.save_global_extras({"limiter": {"enabled": False}})
    return manager


def make_service(directory, manager):
    from audio.output_service import OutputService, OutputServiceDeps
    return OutputService(OutputServiceDeps(
        store=OutputStateStore(Path(directory) / "output-state.json"),
        preset_loader=manager.preset_store.read,
        resolve_ir=lambda kernel: (_ for _ in ()).throw(AssertionError(kernel)),
        measurement_active=lambda: False,
    ))


def seed_sub_state(service):
    return service.apply(
        lambda state: switch_mode(set_mode_routing(state, "stereo-sub", "A", ["main_l", "main_r", "sub1", "sub1"]), "stereo-sub"),
        expected_revision=0)


def plan_for(service, state, rate=48000):
    return service.compile_plan(state, output_key="A", channels=4, sample_rate_hz=rate)


PORTS = [f"playback_AUX{i}" for i in range(4)]


def target_for(service, manager, state, *, fingerprint="fp-new"):
    plan = plan_for(service, state)
    layout = service.compile_layout(plan)
    config = DSPRuntimeConfig.from_plan(
        plan, layout=layout, output_key="A", sample_rate_hz=48000,
        hardware_ports=PORTS, plan_fingerprint=fingerprint)
    text = manager.compile_engine_text(
        [dict(entry) for entry in layout], preset_name=plan["global"]["preset"],
        sample_rate_hz=48000, extras_override=plan["global"]["extras"])
    return PlannedSyncTarget(config=config, text=text)


class FromPlanTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = make_manager(directory.name)
        self.service = make_service(directory.name, self.manager)
        self.state = seed_sub_state(self.service)

    def test_maps_roles_and_physical_fanout_edges(self):
        target = target_for(self.service, self.manager, self.state)
        config = target.config
        self.assertEqual(config.output_key, "A")
        self.assertEqual(config.sample_rate, 48000)
        self.assertEqual(config.hardware_ports, tuple(PORTS))
        self.assertEqual([row["name"] for row in config.layout], ["main_l", "main_r", "sub1"])
        self.assertEqual(config.route_pairs,
                         ((1, "playback_AUX0"), (2, "playback_AUX1"),
                          (3, "playback_AUX2"), (3, "playback_AUX3")))
        self.assertEqual(config.plan_fingerprint, "fp-new")
        self.assertIn("sos 2 ", target.text)

    def test_rejects_missing_ports_and_out_of_range_channels(self):
        plan = plan_for(self.service, self.state)
        layout = self.service.compile_layout(plan)
        with self.assertRaises(RuntimeError):
            DSPRuntimeConfig.from_plan(plan, layout=layout, output_key="A",
                                       sample_rate_hz=48000, hardware_ports=[])
        with self.assertRaises(RuntimeError):
            DSPRuntimeConfig.from_plan(plan, layout=layout, output_key="A",
                                       sample_rate_hz=48000, hardware_ports=PORTS[:2])

    def test_snapshot_exposes_plan_fingerprint(self):
        async def run():
            runtime = DSPRuntime(self.manager, command_runner=_unreachable)
            runtime._config = target_for(self.service, self.manager, self.state).config
            self.assertEqual(runtime.snapshot()["config"]["plan_fingerprint"], "fp-new")
            legacy = DSPRuntimeConfig.from_overview(
                {"output_mode": {"mode": "stereo", "effective_output_key": "A",
                                 "effective_output_channels": 2, "effective_output_rate": 48000}})
            runtime._config = legacy
            self.assertIsNone(runtime.snapshot()["config"]["plan_fingerprint"])
        asyncio.run(run())

    def test_link_build_ignores_overview_mode_label(self):
        """Task-3 contract: plan link build takes only plan+ports.

        ``from_plan`` accepts no overview input by construction; the per-label
        overview here is the sketch's decoy proving the label is never
        consulted. Route pairs must equal the plan target for every label.
        """
        from dsp.runtime import DSPRuntimeConfig
        target = target_for(self.service, self.manager, self.state)
        plan = plan_for(self.service, self.state)
        layout = self.service.compile_layout(plan)
        for label in ("stereo", "subwoofer-2.1", "subwoofer-2.2", "crossover", ""):
            overview = {"output_mode": {"mode": label,
                        "effective_output_key": "A", "effective_output_channels": 4,
                        "hardware_playback_ports": list(PORTS)}}
            self.assertEqual(overview["output_mode"]["mode"], label)
            config = DSPRuntimeConfig.from_plan(
                plan, layout=layout, output_key="A", sample_rate_hz=48000,
                hardware_ports=list(PORTS), plan_fingerprint="fp-new")
            self.assertEqual(config.route_pairs, target.config.route_pairs)


async def _unreachable(command):
    raise AssertionError(f"unexpected command: {command[0]}")


def pw_link_io(*ports):
    lines = []
    for port in ports:
        lines.append(f"fxroute_dsp:{port}")
    return "\n".join(lines)


class SyncRenderedTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.manager = make_manager(directory.name)
        self.service = make_service(directory.name, self.manager)
        self.state = seed_sub_state(self.service)
        self.staged = []

    def make_runtime(self, preflight_rc=0):
        staged = self.staged

        async def run(command):
            if str(command[0]).endswith("fxroute-dsp-offline"):
                staged.append(Path(command[1]).read_text())
                return CommandResult(preflight_rc, "" if preflight_rc == 0 else "bad sos")
            if list(command[:2]) == ["pw-link", "-io"]:
                return CommandResult(0, pw_link_io("input_1", "input_2", "output_1", "output_2",
                                                  "output_3", "post_effect_FL", "post_effect_FR"))
            if list(command[:2]) == ["pw-link", "-l"]:
                return CommandResult(0, "\n".join(
                    f"fxroute_dsp:output_{signal} -> A:{port}"
                    for signal, port in ((1, "playback_AUX0"), (2, "playback_AUX1"),
                                         (3, "playback_AUX2"), (3, "playback_AUX3"))))
            return CommandResult(0)

        async def launch(command):
            return FakeProcess()

        binary = Path(self.directory) / "fxroute-dsp"
        binary.touch()
        runtime = DSPRuntime(self.manager, binary=binary, command_runner=run,
                             process_launcher=launch)
        runtime._control = mock.AsyncMock(return_value="0")
        return runtime

    def test_sync_rendered_stages_text_and_links(self):
        async def run():
            runtime = self.make_runtime()
            target = target_for(self.service, self.manager, self.state)
            await runtime.sync_rendered(target, initial_output_gain_db=0.0)
            self.assertTrue(runtime.snapshot()["active"])
            self.assertEqual(runtime.snapshot()["config"]["plan_fingerprint"], "fp-new")
            self.assertEqual(len(self.staged), 1)
            self.assertIn("sos 2 ", self.staged[0])
            links = {(link.source, link.target) for link in runtime._links
                     if link.source.startswith("fxroute_dsp:output_")}
            self.assertEqual(links, {
                ("fxroute_dsp:output_1", "A:playback_AUX0"),
                ("fxroute_dsp:output_2", "A:playback_AUX1"),
                ("fxroute_dsp:output_3", "A:playback_AUX2"),
                ("fxroute_dsp:output_3", "A:playback_AUX3"),
            })
        asyncio.run(run())

    def test_guarded_rebuild_rolls_back_to_previous_target(self):
        async def run():
            runtime = self.make_runtime()
            leveled = set_bank_preset(
                set_output_processing(self.state, "stereo-sub", "sub1", level_db=-4.0),
                "stereo-sub", "sub1", preset="Room")
            new_target = target_for(self.service, self.manager, leveled, fingerprint="fp-new")
            old_target = target_for(self.service, self.manager, self.state, fingerprint="fp-old")
            await runtime.sync_rendered(old_target, initial_output_gain_db=0.0)
            calls = []
            original_run = runtime._run

            async def flaky_run(command):
                if str(command[0]).endswith("fxroute-dsp-offline"):
                    calls.append(Path(command[1]).read_text())
                    if len(calls) == 1:
                        return CommandResult(1, "bad sos")
                    return CommandResult(0)
                return await original_run(command)

            runtime._run = flaky_run
            with self.assertRaises(RuntimeError):
                await runtime.guarded_rebuild_rendered(
                    new_target, previous=old_target, guard_db=-2.0,
                    apply_candidate=lambda: None, apply_previous=lambda: None,
                    settle_seconds=0.0)
            self.assertEqual(len(calls), 2)
            self.assertIn("output 2 -4 ", calls[0])
            self.assertNotIn("output 2 -4 ", calls[1])
            self.assertEqual(runtime.snapshot()["config"]["plan_fingerprint"], "fp-old")
        asyncio.run(run())


class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def lifecycle_context(service, manager, runtime):
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.multiple(
        main,
        get_output_service=mock.MagicMock(return_value=service),
        measurement_sr_session=mock.MagicMock(has_active_jobs=False),
        get_audio_output_overview=mock.MagicMock(return_value={
            "selected_output": {"key": "A", "channels": 4},
            "output_mode": {"effective_output_key": "A", "effective_output_channels": 4,
                            "hardware_playback_ports": PORTS},
        }),
        get_samplerate_status=mock.MagicMock(return_value={"active_rate": 48000}),
        _require_dsp_manager=mock.MagicMock(return_value=manager),
    ))
    stack.enter_context(mock.patch.object(main.runtime, "dsp_runtime", runtime))
    return stack


class ApplyLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.manager = make_manager(directory.name)
        self.service = make_service(directory.name, self.manager)
        seed_sub_state(self.service)
        self.runtime = mock.MagicMock()
        self.runtime.snapshot.return_value = {"output_gain_db": 0.0}
        self.runtime.guarded_rebuild_rendered = mock.AsyncMock()
        self.runtime.sync_rendered = mock.AsyncMock()

    def test_fast_path_rebuilds_same_topology(self):
        with lifecycle_context(self.service, self.manager, self.runtime):
            result = asyncio.run(main.apply_audio_output_state(FakeRequest({
                "expected_revision": 1,
                "mutation": {"kind": "set_bank_preset", "mode": "stereo-sub",
                             "bank_id": "sub1", "preset": "Room"},
            })))
        self.assertEqual(result["revision"], 2)
        self.assertTrue(result["live_applied"])
        self.assertIsNone(result["live_reason"])
        self.assertTrue(result["fingerprint_changed"])
        self.runtime.guarded_rebuild_rendered.assert_awaited_once()
        target = self.runtime.guarded_rebuild_rendered.await_args.args[0]
        self.assertEqual(target.config.plan_fingerprint, result["fingerprint"])
        self.assertIn("sos 2 ", target.text)
        self.runtime.sync_rendered.assert_not_awaited()

    def test_failed_rebuild_restores_bytes_and_previous_graph(self):
        self.runtime.guarded_rebuild_rendered = mock.AsyncMock(
            side_effect=RuntimeError("staging failed"))
        with lifecycle_context(self.service, self.manager, self.runtime):
            with self.assertRaises(main.HTTPException) as ctx:
                asyncio.run(main.apply_audio_output_state(FakeRequest({
                    "expected_revision": 1,
                    "mutation": {"kind": "set_bank_preset", "mode": "stereo-sub",
                                 "bank_id": "sub1", "preset": "Room"},
                })))
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(self.service.load()["revision"], 3)
        self.assertEqual(self.service.load()["modes"]["stereo-sub"]["banks"]["sub1"]["preset"], "Neutral")
        self.runtime.sync_rendered.assert_awaited_once()
        rollback = self.runtime.sync_rendered.await_args.args[0]
        self.assertEqual(rollback.config.plan_fingerprint,
                         self.service.fingerprint(self.service.load(), output_key="A",
                                                  channels=4, sample_rate_hz=48000))

    def test_selection_only_change_skips_rebuild(self):
        with lifecycle_context(self.service, self.manager, self.runtime):
            result = asyncio.run(main.apply_audio_output_state(FakeRequest({
                "expected_revision": 1,
                "mutation": {"kind": "select_bank", "mode": "stereo-sub", "bank_id": "sub1"},
            })))
        self.assertEqual(result["revision"], 2)
        self.assertFalse(result["live_applied"])
        self.assertEqual(result["live_reason"], "nothing-to-apply")
        self.assertFalse(result["fingerprint_changed"])
        self.runtime.guarded_rebuild_rendered.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
