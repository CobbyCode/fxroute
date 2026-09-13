#!/usr/bin/env python3
"""Saved output survives default drift, extra links and device recreation."""

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio import system_volume
from audio.samplerate import overview, persistence
from dsp.runtime import DSPRuntime, DSPRuntimeConfig, PipeWireLink, CommandResult
from dsp.orchestration import DspOrchestrator
from test_link_watch_output_mode_gate import _watcher_deps


class OutputAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = mock.patch.dict(os.environ, XDG_CONFIG_HOME=temp.name)
        env.start()
        self.addCleanup(env.stop)
        persistence._save_audio_output_selection("alsa_output.A")
        self.default = "alsa_output.B"
        self.volumes = {"alsa_output.A": 27, "alsa_output.B": 30}
        self.desired = {
            PipeWireLink("fxroute_dsp:output_1", "alsa_output.A:playback_FL"),
            PipeWireLink("fxroute_dsp:output_2", "alsa_output.A:playback_FR"),
        }
        self.extra = PipeWireLink("fxroute_dsp:output_1", "alsa_output.B:playback_FL")
        self.tap = PipeWireLink("fxroute_dsp:post_effect_FL", "meter:input_FL")
        self.links = self.desired | {self.extra, self.tap}
        self.runtime = DSPRuntime(None, command_runner=self.run_graph)
        self.runtime._config = DSPRuntimeConfig("stereo", "alsa_output.A", 44100,
                                                ("playback_FL", "playback_FR"), ())
        self.runtime._links = list(self.desired)
        self.runtime._process = SimpleNamespace(returncode=None, pid=123)
        for module in (overview, system_volume):
            patch = mock.patch.object(module, "_run_command", side_effect=self.command)
            patch.start()
            self.addCleanup(patch.stop)
        system_volume._status_volume_cache = None
        self.addCleanup(setattr, system_volume, "_status_volume_cache", None)

    def command(self, args):
        args = list(args)
        if args == ["pactl", "get-default-sink"]:
            return self.default
        if args[:2] == ["pactl", "set-default-sink"]:
            self.default = args[2]
            return ""
        if args == ["pactl", "list", "sinks", "short"]:
            return "\n".join(f"{i}\t{name}\tPipeWire\ts32le 2ch 44100Hz\tRUNNING"
                             for i, name in enumerate(self.volumes, 70))
        if args[:2] in (["pactl", "get-sink-volume"], ["pactl", "set-sink-volume"],
                        ["wpctl", "get-volume"], ["wpctl", "set-volume"]):
            target = self.default if args[2] == "@DEFAULT_AUDIO_SINK@" else args[2]
            if target not in self.volumes:
                raise system_volume.SystemVolumeError("Selected sink unavailable")
            if args[1].startswith("set-"):
                self.volumes[target] = int(args[3].rstrip("%"))
                return ""
            value = self.volumes[target]
            return (f"Volume: front-left: 16384 / {value}% / -36.12 dB, "
                    f"front-right: 16384 / {value}% / -36.12 dB\n balance 0.00"
                    if args[0] == "pactl" else f"Volume: {value / 100:.2f}")
        raise AssertionError(f"Unexpected command: {args}")

    async def run_graph(self, args):
        args = tuple(args)
        if args == ("pw-link", "-l"):
            return CommandResult(0, "\n".join(f"{link.source}\n  |-> {link.target}" for link in sorted(self.links, key=str)))
        if args[:2] == ("pw-link", "-d"):
            self.links.discard(PipeWireLink(*args[2:]))
            return CommandResult(0)
        if args[:1] == ("pw-link",) and len(args) == 3:
            self.links.add(PipeWireLink(*args[1:]))
            return CommandResult(0)
        raise AssertionError(f"Unexpected graph command: {args}")

    def test_master_controls_saved_a_even_while_default_is_b(self):
        self.assertEqual(system_volume.set_output_volume(25), 25)
        self.assertEqual(self.volumes, {"alsa_output.A": 25, "alsa_output.B": 30})
        self.assertEqual(system_volume.get_output_volume(), 25)
        self.assertEqual(system_volume.get_status_volume(), 25)

    def test_missing_selected_device_never_changes_b(self):
        del self.volumes["alsa_output.A"]
        with self.assertRaises(system_volume.SystemVolumeError):
            system_volume.set_output_volume(20)
        self.assertEqual(self.volumes["alsa_output.B"], 30)

    def test_samplerate_status_prefers_selection_over_running_default(self):
        sinks = [{"name": "alsa_output.B", "state": "RUNNING", "active_rate": 48000},
                 {"name": "alsa_output.A", "state": "RUNNING", "active_rate": 44100}]
        self.assertEqual(overview._select_relevant_sink({"name": self.default}, sinks)["active_rate"], 44100)

    async def test_repair_removes_untracked_b_and_restores_recreated_a_links(self):
        self.links -= self.desired
        await self.runtime.reclean_direct_dsp_links()
        self.assertEqual(self.links, self.desired | {self.tap})
        self.assertTrue(await self.runtime.verify())

    async def test_extra_physical_output_is_not_a_complete_graph(self):
        self.assertFalse(await self.runtime.verify())

    def test_default_drift_and_reappearing_device_reconcile_to_saved_a(self):
        reconcile = getattr(overview, "reconcile_selected_output_default", None)
        self.assertTrue(callable(reconcile), "Output default reconciliation is required")
        self.assertEqual(reconcile(), "alsa_output.A")
        self.assertEqual(self.default, "alsa_output.A")
        self.default = "alsa_output.B"
        del self.volumes["alsa_output.A"]
        self.assertIsNone(reconcile())
        self.assertEqual(persistence._load_audio_output_selection()["selected_key"], "alsa_output.A")
        self.volumes["alsa_output.A"] = 25
        self.assertEqual(reconcile(), "alsa_output.A")
        self.assertEqual(self.default, "alsa_output.A")

    async def test_idle_stereo_watcher_restores_default_and_exclusive_output(self):
        self.links -= self.desired
        ticks = 0

        async def sleep(_delay):
            nonlocal ticks
            ticks += 1
            if ticks > 1:
                raise asyncio.CancelledError

        deps = _watcher_deps(SimpleNamespace(
            sleep=sleep, get_dsp_runtime=lambda: self.runtime,
            get_audio_output_overview=lambda: self.fail("Healthy stereo needs no overview"),
            observe_playback_samplerate_drift=mock.AsyncMock(), get_output_mode=lambda: "stereo"))
        coord_lock = asyncio.Lock()
        deps = replace(deps, reconcile_output_default=overview.reconcile_selected_output_default,
                       playback_transition_is_active=lambda: coord_lock.locked(),
                       get_coordinator_lock=lambda: coord_lock,
                       get_measurement_sr_session=lambda: SimpleNamespace(lock=asyncio.Lock()))
        with self.assertRaises(asyncio.CancelledError):
            await DspOrchestrator(deps).runtime_link_watch_loop()
        self.assertEqual(self.default, "alsa_output.A")
        self.assertEqual(self.links, self.desired | {self.tap})

    async def test_watcher_skips_repair_while_coordinator_transition_runs(self):
        coord_lock = asyncio.Lock()
        await coord_lock.acquire()
        try:
            deps = _watcher_deps(SimpleNamespace(
                sleep=lambda _d: None, get_dsp_runtime=lambda: self.runtime,
                get_audio_output_overview=lambda: self.fail("must skip"),
                observe_playback_samplerate_drift=mock.AsyncMock(), get_output_mode=lambda: "stereo"))
            deps = replace(deps, reconcile_output_default=overview.reconcile_selected_output_default,
                           playback_transition_is_active=lambda: True,
                           get_coordinator_lock=lambda: coord_lock,
                           get_measurement_sr_session=lambda: SimpleNamespace(lock=asyncio.Lock()))
            await DspOrchestrator(deps).reconcile_output_authority()
        finally:
            coord_lock.release()
        self.assertEqual(self.default, "alsa_output.B")
        self.assertIn(self.extra, self.links)

    async def test_watcher_holds_coordinator_lock_during_default_repair(self):
        coord_lock = asyncio.Lock()
        seen_locked = []

        def slow_reconcile():
            seen_locked.append(coord_lock.locked())
            return overview.reconcile_selected_output_default()

        deps = _watcher_deps(SimpleNamespace(
            sleep=lambda _d: None, get_dsp_runtime=lambda: self.runtime,
            get_audio_output_overview=lambda: self.fail("must skip"),
            observe_playback_samplerate_drift=mock.AsyncMock(), get_output_mode=lambda: "stereo"))
        deps = replace(deps, reconcile_output_default=slow_reconcile,
                       playback_transition_is_active=lambda: coord_lock.locked(),
                       get_coordinator_lock=lambda: coord_lock,
                       get_measurement_sr_session=lambda: SimpleNamespace(lock=asyncio.Lock()))
        acquire_attempts = []

        async def competing_transition():
            await asyncio.sleep(0)
            acquire_attempts.append(True)
            await coord_lock.acquire()
            coord_lock.release()

        task = asyncio.create_task(competing_transition())
        await DspOrchestrator(deps).reconcile_output_authority()
        await task
        self.assertEqual(seen_locked, [True])
        self.assertEqual(self.default, "alsa_output.A")

    async def test_destroyed_ports_escalate_from_link_repair_to_rebuild(self):
        self.links -= self.desired
        original_run = self.run_graph

        async def failing_run(args):
            args = tuple(args)
            if args[:1] == ("pw-link",) and len(args) == 3:
                return CommandResult(1, "", "No such port")
            return await original_run(args)

        self.runtime._run = failing_run
        deps = _watcher_deps(SimpleNamespace(
            sleep=lambda _d: None, get_dsp_runtime=lambda: self.runtime,
            get_audio_output_overview=lambda: self.fail("must skip"),
            observe_playback_samplerate_drift=mock.AsyncMock(), get_output_mode=lambda: "stereo"))
        coord_lock = asyncio.Lock()
        deps = replace(deps, reconcile_output_default=overview.reconcile_selected_output_default,
                       playback_transition_is_active=lambda: coord_lock.locked(),
                       get_coordinator_lock=lambda: coord_lock,
                       get_measurement_sr_session=lambda: SimpleNamespace(lock=asyncio.Lock()))
        orchestrator = DspOrchestrator(deps)
        rebuilds = []
        orchestrator.sync_runtime = mock.AsyncMock(side_effect=lambda **_k: rebuilds.append(True))
        await orchestrator.reconcile_output_authority()
        self.assertEqual(rebuilds, [True])


if __name__ == "__main__":
    unittest.main()
