#!/usr/bin/env python3
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp_manager import DSPManager
from dsp_runtime import CommandResult, DSPRuntime, DSPRuntimeConfig, DSP_INGRESS_MONITOR_NODE


class DSPRuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.manager = DSPManager(home=Path(tempfile.mkdtemp()))

    def overview(self, mode):
        block = {"mode": mode, "effective_output_key": "hw", "effective_output_channels": 8,
                 "effective_output_rate": 48000}
        if mode == "subwoofer-2.1":
            block["subwoofer"] = {"crossover_frequency_hz": 80, "main_highpass_enabled": True,
                                  "sub_level_db": -2, "sub_alignment_ms": 3, "sub_polarity": "invert"}
        if mode.startswith("subwoofer-2.2"):
            block.update({"crossover_frequency_hz": 90, "main_highpass_enabled": True,
                          "subwoofers": {"sub1": {"level_db": -1, "alignment_ms": -2, "polarity": "normal"},
                                          "sub2": {"level_db": -3, "alignment_ms": 4, "polarity": "invert"}}})
        return {"output_mode": block, "selected_output": {"key": "hw", "channels": 8}}

    def test_stereo_is_two_output_identity_matrix(self):
        config = DSPRuntimeConfig.from_overview(self.overview("stereo"))
        self.assertEqual(config.hardware_ports, ("playback_FL", "playback_FR"))
        self.assertEqual(config.layout[0]["routes"], [{"input": 0, "gain": 1.0}])
        self.assertEqual(config.layout[1]["routes"], [{"input": 1, "gain": 1.0}])

    def test_21_uses_lr24_and_mono_sparse_bass_matrix(self):
        config = DSPRuntimeConfig.from_overview(self.overview("subwoofer-2.1"))
        self.assertEqual(config.hardware_ports, ("playback_FL", "playback_FR", "playback_RL", "playback_RR"))
        self.assertEqual(config.layout[2]["routes"], [{"input": 0, "gain": .5}, {"input": 1, "gain": .5}])
        self.assertEqual(config.layout[2]["filters"], [{"type": "lowpass", "frequency_hz": 80, "q": 0.70710678, "stages": 2}])
        self.assertTrue(config.layout[2]["invert"])

    def test_22_stereo_keeps_left_and_right_bass_separate(self):
        config = DSPRuntimeConfig.from_overview(self.overview("subwoofer-2.2-stereo"))
        self.assertEqual(config.layout[2]["routes"], [{"input": 0, "gain": 1.0}])
        self.assertEqual(config.layout[3]["routes"], [{"input": 1, "gain": 1.0}])

    def test_engine_text_contains_layout_crossovers(self):
        config = DSPRuntimeConfig.from_overview(self.overview("subwoofer-2.1"))
        text = self.manager.compile_engine_text(list(config.layout), sample_rate_hz=config.sample_rate)
        self.assertEqual(text.count("peq 0 highpass 80"), 2)
        self.assertEqual(text.count("peq 2 lowpass 80"), 2)

    def test_ingress_links_use_exported_null_sink_monitor_ports(self):
        self.assertEqual(DSP_INGRESS_MONITOR_NODE, "fxroute_dsp_sink")

    def test_reclean_removes_direct_player_hardware_links(self):
        commands = []

        async def run(command):
            commands.append(tuple(command))
            return CommandResult(0)

        runtime = DSPRuntime(self.manager, command_runner=run)
        runtime._config = DSPRuntimeConfig.from_overview(self.overview("stereo"))
        asyncio.run(runtime._remove_direct_source_links())
        self.assertIn(("pw-link", "-d", "mpv:output_FL", "hw:playback_FL"), commands)
        self.assertIn(("pw-link", "-d", "spotify:output_RR", "hw:playback_RR"), commands)

    def test_reclean_waits_for_runtime_lock(self):
        events = []

        async def run(command):
            events.append(tuple(command))
            return CommandResult(0, "")

        async def exercise():
            runtime = DSPRuntime(self.manager, command_runner=run)
            runtime._config = DSPRuntimeConfig.from_overview(self.overview("stereo"))
            await runtime._lock.acquire()
            task = asyncio.create_task(runtime._reclean_guarded())
            await asyncio.sleep(0)
            self.assertEqual(events, [])
            runtime._lock.release()
            await task

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
