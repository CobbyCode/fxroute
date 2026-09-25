#!/usr/bin/env python3
"""Output selection commits only with the verified runtime graph."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import audio.samplerate.overview as output_overview
from audio.output_state import default_output_state, set_mode_routing, switch_mode
from audio.samplerate import _load_audio_output_selection, _save_audio_output_selection


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
        return {"output_key": key, "links_complete": self.state["graph"] == key}

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
            return {"output_key": key, "links_complete": key == "A"}

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
