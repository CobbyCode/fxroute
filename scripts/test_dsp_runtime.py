#!/usr/bin/env python3
import asyncio
import json
import os
import re
import signal
import socket
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp.manager import DSPManager
from dsp.runtime import (CommandResult, DSPRuntime, DSPRuntimeConfig, PipeWireLink,
                         PlannedSyncTarget, CONTROL_REPLY_MAX_BYTES, DSP_INGRESS_MONITOR_NODE,
                         RUNTIME_COMMAND_TIMEOUT_RETURNCODE,
                         RUNTIME_COMMAND_TIMEOUT_SECONDS, _contains_link,
                         iter_pw_links, physical_output_links,
                         pw_link_output_unrecognized)

class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.pid = 123
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    async def wait(self):
        return self.returncode

class DSPRuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.manager = DSPManager(home=Path(tempfile.mkdtemp()))
        self.manager.save_global_extras({"limiter": {"enabled": False}})

    PORTS = ("playback_FL", "playback_FR", "playback_RL", "playback_RR")

    def config(self, mode="stereo", output_key="hw"):
        """Plan-shaped config: one layout row and one physical route per output."""
        highpass = [{"type": "highpass", "frequency_hz": 80, "q": 0.70710678, "stages": 2}]
        rows = [{"name": "main_l", "routes": [{"input": 0, "gain": 1.0}]},
                {"name": "main_r", "routes": [{"input": 1, "gain": 1.0}]}]
        if mode == "stereo-sub":
            for row in rows:
                row["filters"] = list(highpass)
            rows.append({"name": "sub1", "routes": [{"input": 0, "gain": .5}, {"input": 1, "gain": .5}],
                         "filters": [{"type": "lowpass", "frequency_hz": 80, "q": 0.70710678, "stages": 2}]})
            rows.append({"name": "sub2", "routes": [{"input": 0, "gain": .5}, {"input": 1, "gain": .5}],
                         "filters": [{"type": "lowpass", "frequency_hz": 80, "q": 0.70710678, "stages": 2}]})
        routes = tuple((index + 1, port) for index, port in enumerate(self.PORTS[:len(rows)]))
        return DSPRuntimeConfig(mode, output_key, 48000, self.PORTS, tuple(rows), routes, "plan")

    def target(self, config=None):
        config = config or self.config()
        return PlannedSyncTarget(config, self.manager.compile_engine_text(
            list(config.layout), sample_rate_hz=config.sample_rate))

    def test_engine_text_contains_layout_crossovers(self):
        config = self.config("stereo-sub")
        text = self.manager.compile_engine_text(list(config.layout), sample_rate_hz=config.sample_rate)
        self.assertEqual(text.count("peq 0 highpass 80"), 2)
        self.assertEqual(text.count("peq 2 lowpass 80"), 2)

    def test_engine_text_emits_every_enabled_stage_in_exact_order(self):
        ir = self.manager.library().irs_dir / "room.irs"
        ir.parent.mkdir(parents=True)
        import wave as _wave
        with _wave.open(str(ir), "wb") as _handle:
            _handle.setnchannels(1)
            _handle.setsampwidth(2)
            _handle.setframerate(48000)
            _handle.writeframes(b"\x00\x00\x00\x00")
        chain = [
            {"id": "delay#0", "type": "delay", "enabled": True,
             "params": {"leftMs": 1, "rightMs": 2}},
            {"id": "equalizer#0", "type": "equalizer", "enabled": True,
             "params": {"channelMode": "stereo-linked", "eqMode": "FIR", "bands": []}},
            {"id": "delay#1", "type": "delay", "enabled": True,
             "params": {"leftMs": 3, "rightMs": 4}},
            {"id": "headroom#0", "type": "headroom", "enabled": True,
             "params": {"gainDb": -2}},
            {"id": "headroom#1", "type": "headroom", "enabled": True,
             "params": {"gainDb": -4}},
            {"id": "convolver#0", "type": "convolver", "enabled": True,
             "params": {"kernel": "room", "wet_db": -1, "dry_db": -9,
                        "input_gain_db": -2, "output_gain_db": 3}},
            {"id": "crystalizer#0", "type": "crystalizer", "enabled": True, "params": {}},
        ]
        self.manager.library().write("Ordered", self.manager._native_preset(chain))
        text = self.manager.compile_engine_text(
            [{"name": "FL", "source": 0}, {"name": "FR", "source": 1}],
            preset_name="Ordered")
        blocks = [line for line in text.splitlines() if line.startswith("stage_begin ")]
        self.assertEqual(blocks, [
            "stage_begin 0 delay#0 native delay",
            "stage_begin 1 equalizer#0 lv2 http://lsp-plug.in/plugins/lv2/para_equalizer_x32_lr",
            "stage_begin 2 delay#1 native delay",
            "stage_begin 3 headroom#0 native headroom",
            "stage_begin 4 headroom#1 native headroom",
            "stage_begin 5 convolver#0 native convolver",
            "stage_begin 6 crystalizer#0 native crystalizer",
        ])
        self.assertIn("param wet_db -1", text)
        self.assertIn("param dry_db -9", text)
        self.assertIn("param input_gain_db -2", text)
        self.assertIn("param output_gain_db 3", text)

    def test_peq_lv2_controls_preserve_dual_channels_mode_and_gains(self):
        chain = [{"id": "equalizer#0", "type": "equalizer", "enabled": True,
                  "mix": {"inputGainDb": -2, "outputGainDb": 3},
                  "params": {"channelMode": "dual", "eqMode": "SPM",
                             "leftBands": [{"enabled": True, "filterType": "bell",
                                            "frequencyHz": 100, "gainDb": -6, "q": 2}],
                             "rightBands": [{"enabled": True, "filterType": "high_shelf",
                                             "frequencyHz": 5000, "gainDb": 4, "q": .7}]}}]
        self.manager.library().write("Dual", self.manager._native_preset(chain))
        text = self.manager.compile_engine_text(
            [{"name": "FL", "source": 0}, {"name": "FR", "source": 1}], preset_name="Dual")
        self.assertNotIn("control clink ", text)
        for expected in ("control g_in 0.794328235", "control g_out 1.41253754",
                          "control mode 3", "control ftl_0 1",
                          "control fml_0 6", "control fmr_0 6", "control sl_0 0",
                          "control fl_0 100",
                          "control gl_0 0.501187234", "control ql_0 2",
                          "control ftr_0 3", "control fr_0 5000",
                          "control gr_0 1.58489319", "control qr_0 0.7",
                          "control ftl_1 0", "control ftr_1 0",
                          "control ftl_31 0", "control ftr_31 0"):
            self.assertIn(expected, text)

    def test_peq_lv2_type_values_match_lsp_metadata(self):
        bands = [{"enabled": True, "filterType": kind, "frequencyHz": 1000,
                  "gainDb": 0, "q": 1}
                 for kind in ("bell", "high_pass", "high_shelf", "low_pass",
                              "low_shelf", "notch")]
        chain = [{"id": "equalizer#0", "type": "equalizer", "enabled": True,
                  "params": {"channelMode": "stereo-linked", "eqMode": "FFT",
                             "bands": bands}}]
        self.manager.library().write("Types", self.manager._native_preset(chain))
        text = self.manager.compile_engine_text(
            [{"name": "FL", "source": 0}, {"name": "FR", "source": 1}], preset_name="Types")
        self.assertIn("control mode 2", text)
        for index, value in enumerate(range(1, 7)):
            self.assertIn(f"control ftl_{index} {value}", text)
            self.assertIn(f"control ftr_{index} {value}", text)

    def test_peq_gain_and_delay_keep_legacy_semantics(self):
        chain = [{"id": "equalizer#0", "type": "equalizer", "enabled": True,
                  "mix": {"inputGainDb": -1, "outputGainDb": 2},
                  "params": {"channelMode": "dual", "eqMode": "IIR",
                             "leftBands": [
                                 {"enabled": True, "filterType": "gain", "gainDb": 3},
                                 {"enabled": True, "filterType": "delay", "delayMs": 4},
                                 {"enabled": True, "filterType": "bell", "frequencyHz": 100,
                                  "gainDb": -2, "q": 1}],
                             "rightBands": [
                                 {"enabled": True, "filterType": "gain", "gainDb": 3},
                                 {"enabled": True, "filterType": "delay", "delayMs": 7},
                                 {"enabled": True, "filterType": "notch", "frequencyHz": 200,
                                  "gainDb": 0, "q": 2}]}}]
        self.manager.library().write("Legacy PEQ", self.manager._native_preset(chain))
        text = self.manager.compile_engine_text(
            [{"name": "FL", "source": 0}, {"name": "FR", "source": 1}],
            preset_name="Legacy PEQ")
        self.assertIn("control g_in 1.25892541", text)
        self.assertIn("control ftl_0 1", text)
        self.assertIn("control ftr_0 6", text)
        self.assertIn("control ftl_1 0", text)
        self.assertIn("stage_begin 1 equalizer#0-delay native delay", text)
        self.assertIn("param left_ms 4", text)
        self.assertIn("param right_ms 7", text)

    def test_peq_delay_entries_accumulate_beyond_global_delay_limit(self):
        chain = [{"id": "equalizer#0", "type": "equalizer", "enabled": True,
                  "params": {"channelMode": "stereo-linked", "bands": [
                      {"enabled": True, "filterType": "delay", "delayMs": 500},
                      {"enabled": True, "filterType": "delay", "delayMs": 500},
                      {"enabled": True, "filterType": "delay", "delayMs": 500},
                  ]}}]
        self.manager.library().write("Long PEQ Delay", self.manager._native_preset(chain))
        text = self.manager.compile_engine_text(
            [{"name": "FL", "source": 0}, {"name": "FR", "source": 1}],
            preset_name="Long PEQ Delay")
        self.assertIn("param left_ms 1500", text)
        self.assertIn("param right_ms 1500", text)

    def test_lv2_effect_mappings_include_complete_controls_and_loudness_coupling(self):
        chain = [
            {"id": "bass_enhancer#0", "type": "bass_enhancer", "enabled": True,
             "params": {"amount": 5, "harmonics": 8.5, "scope": 100, "blend": 2}},
            {"id": "autogain#0", "type": "autogain", "enabled": True,
             "params": {"targetDb": -18, "reference": "Geometric Mean (MSI)",
                        "silenceThresholdDb": -65, "maximumHistorySeconds": 12}},
            {"id": "loudness#0", "type": "loudness", "enabled": True,
             "params": {"fftSize": 8192, "strength": 7, "volumeDb": -20,
                        "calibration": {"requiredAdjustmentDb": 2.5}}},
            {"id": "limiter#0", "type": "limiter", "enabled": True,
             "params": {"inputGainDb": -3, "outputGainDb": 2,
                        "thresholdDb": -2, "attackMs": 4, "releaseMs": 60,
                         "lookaheadMs": 6, "stereoLinkPercent": 80}},
            {"id": "maximizer#0", "type": "maximizer", "enabled": True,
             "params": {"thresholdDb": -0.5, "releaseMs": 30, "inputGainDb": 1}},
        ]
        self.manager.library().write("LV2", self.manager._native_preset(chain))
        text = self.manager.compile_engine_text(
            [{"name": "FL", "source": 0}, {"name": "FR", "source": 1}], preset_name="LV2")
        self.assertIn("lv2 http://calf.sourceforge.net/plugins/BassEnhancer", text)
        for expected in ("control amount 1.77827941", "control drive 8.5",
                         "control freq 100", "control blend 2", "control listen 0",
                         "control floor_active 0", "control floor 20"):
            self.assertIn(expected, text)
        self.assertIn("lv2 http://lsp-plug.in/plugins/lv2/loud_comp_stereo", text)
        for expected in ("control input 1", "control std 4", "control fft 5",
                         "control volume -7.5",
                         "control hclip 0", "control hcrange 6"):
            self.assertIn(expected, text)
        self.assertNotIn("control mode 0\n", text[text.index("global-loudness"):text.index("param output_gain_db")])
        self.assertNotIn("control approx ", text[text.index("global-loudness"):text.index("param output_gain_db")])
        self.assertIn("param output_gain_db 7.5", text)
        self.assertIn("lv2 http://lsp-plug.in/plugins/lv2/sc_limiter_stereo", text)
        for expected in ("control g_in 0.707945784", "control g_out 1.25892541",
                          "control th 0.794328235", "control at 4", "control rt 20",
                          "control lk 6", "control slink 80", "control alr 0",
                          "control boost 1", "control extsc 0", "control mode 0",
                          "control ovs 0", "control dith 0", "control scp 1"):
            self.assertIn(expected, text)
        for forbidden in ("control smooth ", "control in2lk ", "control in2sc ",
                          "control lk2in ", "control lk2sc ", "control sc2in ",
                          "control sc2lk "):
            self.assertNotIn(forbidden, text)
        self.assertIn("lv2 urn:zamaudio:ZaMaximX2", text)
        self.assertIn("control thresh -0.5", text)
        self.assertIn("control rel 30", text)

    def test_output_alignment_follows_chain(self):
        chain = [{"id": "delay#0", "type": "delay", "enabled": True,
                  "params": {"leftMs": 1, "rightMs": 2}}]
        self.manager.library().write("Placement", self.manager._native_preset(chain))
        text = self.manager.compile_engine_text([{
            "name": "FL", "source": 0, "gain_db": -3, "delay_ms": 4, "invert": True,
            "filters": [{"type": "highpass", "frequency_hz": 80, "q": .707, "stages": 1}],
        }], preset_name="Placement")
        self.assertLess(text.index("stage_end"), text.index("output 0 -3 4 invert"))

    def test_ingress_links_use_exported_null_sink_monitor_ports(self):
        self.assertEqual(DSP_INGRESS_MONITOR_NODE, "fxroute_dsp_sink")

    def test_runtime_controls_effect_bypass_and_output_gain(self):
        commands = []

        async def exercise():
            runtime = DSPRuntime(self.manager)
            async def control(command, *, reply):
                commands.append((command, reply))
                return "0\n" if command.endswith("get") else "ok\n"
            runtime._control_unlocked = control
            previous = await runtime.set_effect_bypass(True)
            self.assertFalse(previous)
            await runtime.set_output_gain_db(-18)
            await runtime.ramp_output_gain_db(-18, 0, step_db=3, interval_seconds=0)

        asyncio.run(exercise())
        self.assertEqual(commands[0][0], "effects bypass get")
        self.assertIn(("effects bypass 1", True), commands)
        self.assertEqual(commands[-1][0], "gain db 0")

    def test_gain_restore_accepts_float32_roundtrip_but_reports_real_drift(self):
        async def exercise(actual):
            runtime = DSPRuntime(self.manager)
            runtime._control_socket = unittest.mock.Mock()

            async def control(command, **kwargs):
                return str(actual) if command == "gain db get" else "ok"

            runtime._control = control
            await runtime._settle_output_gain_db(-12.34567)

        with self.assertNoLogs("dsp.runtime", level="WARNING"):
            asyncio.run(exercise(-12.3456688))
        with self.assertLogs("dsp.runtime", level="WARNING") as logs:
            asyncio.run(exercise(-12.34))
        self.assertTrue(any("restore did not land" in line for line in logs.output))

    def test_hot_update_swaps_compatible_config_without_rebuild(self):
        events = []

        async def exercise():
            async def complete():
                return CommandResult(0)
            with tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / "fxroute-dsp"
                binary.touch()
                runtime = DSPRuntime(self.manager, binary=binary)
                runtime._process = FakeProcess()
                runtime._control_socket = unittest.mock.Mock()
                runtime._config = self.config()
                runtime._run = lambda _command: complete()
                runtime._stop_orphan_helpers = lambda: complete()
                runtime._reconcile_output_links = lambda _config: complete()
                runtime._control = lambda command, **kwargs: record_async(events, ("control", command.split()[0:2]))
                await runtime.sync_rendered(self.target())

        async def record_async(target, event):
            target.append(event)

        asyncio.run(exercise())
        self.assertEqual(events, [("control", ["swap", "config"])])

    def test_live_update_sends_only_changed_stage_values(self):
        async def exercise():
            runtime = DSPRuntime(self.manager)
            runtime._config_text = "stage_begin 0 global-headroom native headroom\nparam gain_db -3\nstage_end\n"
            commands = []

            async def control(command, **_kwargs):
                commands.append(command)
                return "ok\n"

            runtime._control = control
            self.assertTrue(await runtime._try_live_update(
                "stage_begin 0 global-headroom native headroom\nparam gain_db -2\nstage_end\n"
            ))
            self.assertEqual(commands, [
                "live begin", "live param global-headroom gain_db -2", "live commit",
            ])

        asyncio.run(exercise())

    def test_failed_stage_aborts_before_atomic_swap(self):
        async def exercise():
            runtime = DSPRuntime(self.manager)
            runtime._config_text = "stage_begin 0 headroom native headroom\nparam gain_db -3\nstage_end\n"
            commands = []

            async def control(command, **_kwargs):
                commands.append(command)
                if command.startswith("live param"):
                    raise RuntimeError("error invalid command")
                return "ok\n"

            runtime._control = control
            self.assertFalse(await runtime._try_live_update(
                "stage_begin 0 headroom native headroom\nparam gain_db -2\nstage_end\n"))
            self.assertEqual(commands, ["live begin", "live param headroom gain_db -2", "live abort"])

        asyncio.run(exercise())

    def test_failed_commit_does_not_try_swap_on_uncertain_engine(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / "fxroute-dsp"
                binary.touch()
                runtime = DSPRuntime(self.manager, binary=binary)
                runtime._process = FakeProcess()
                runtime._control_socket = unittest.mock.Mock()
                runtime._config = self.config()
                runtime._config_text = "stage_begin 0 headroom native headroom\nparam gain_db -3\nstage_end\n"
                commands = []

                async def control(command, **_kwargs):
                    commands.append(command)
                    if command == "live commit":
                        runtime._poison_control()
                        raise RuntimeError("error live commit timeout")
                    if command == "effects bypass get":
                        return "0\n"
                    return "ok\n"

                async def complete(*_args):
                    return CommandResult(0)

                runtime._control = control
                runtime._run = complete
                runtime._stop_orphan_helpers = complete
                runtime._wait_for_ports = complete
                runtime._remove_direct_source_links = complete
                runtime._reconcile_output_links = complete
                runtime.set_output_gain_db = complete
                runtime._launch = lambda _args: launch()

                async def launch():
                    return FakeProcess()

                await runtime._run_sync(runtime._config,
                    "stage_begin 0 headroom native headroom\nparam gain_db -2\nstage_end\n")
                self.assertNotIn("swap config", " ".join(commands))
                self.assertFalse(runtime._control_poisoned)
                self.assertIn("live commit", commands)
                self.assertEqual(runtime._config_text,
                    "stage_begin 0 headroom native headroom\nparam gain_db -2\nstage_end\n")

        asyncio.run(exercise())

    def test_incompatible_config_still_uses_rebuild_path(self):
        runtime = DSPRuntime(self.manager)
        runtime._config = self.config()
        self.assertFalse(runtime._can_hot_update(self.config(output_key="other-hw")))

    def test_sync_reads_src_only_from_the_reconciled_output(self):
        from audio.src_latency import graph_src_latency
        from test_av_sync_src import node, link

        async def exercise(hot):
            with tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / "fxroute-dsp"
                binary.touch()
                edges = set()
                applied = []

                async def run(command):
                    if tuple(command) == ("pw-link", "-l"):
                        return CommandResult(0, "\n".join(f"{a}\n  |-> {b}" for a, b in edges))
                    if command[0] == "pw-link" and len(command) == 3:
                        edges.add(tuple(command[1:]))
                    return CommandResult(0)

                async def control(command, **_kwargs):
                    if command.startswith("input latency "):
                        applied.append(float(command.split()[-1]))
                    return "0" if command == "effects bypass get" else "ok"

                async def complete(*_args):
                    pass

                async def launch(_command):
                    return FakeProcess()

                runtime = DSPRuntime(self.manager, binary=binary, command_runner=run, process_launcher=launch)
                runtime._process = FakeProcess()
                runtime._control_socket = unittest.mock.Mock()
                runtime._config = self.config(output_key="new" if hot else "vanished")
                runtime._stop_orphan_helpers = complete
                runtime._wait_for_ports = complete
                runtime._remove_direct_source_links = complete
                runtime._control = control
                runtime._control_unlocked = control

                def latency():
                    value = [node(2, 'fxroute_dsp_sink', 'Audio/Sink'), node(3, 'fxroute_dsp'),
                             node(4, 'new', 'Audio/Sink', 250000000), link(2, 3)]
                    if ('fxroute_dsp:output_1', 'new:playback_FL') in edges:
                        value.append(link(3, 4))
                    return graph_src_latency(value)

                runtime.configure_latency_tracking(input_latency_provider=latency)
                try:
                    await runtime.sync_rendered(self.target(self.config(output_key="new")))
                    self.assertEqual(runtime._config.output_key, "new")
                    self.assertEqual(applied, [250])
                    self.assertEqual(runtime._last_input_latency_ms, 250)
                finally:
                    await runtime.stop()

        for hot in (False, True):
            with self.subTest(hot=hot):
                asyncio.run(exercise(hot))

    def test_same_plan_helper_restart_repushes_unchanged_input_latency(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / "fxroute-dsp"
                binary.touch()
                previous = FakeProcess()
                previous.returncode = 1
                commands = []
                async def run(_args):
                    return CommandResult(0)
                async def launch(_args):
                    return FakeProcess()
                async def complete(*_args):
                    pass
                async def control(command, **_kwargs):
                    commands.append(command)
                    return "0" if command == "effects bypass get" else "ok"
                runtime = DSPRuntime(self.manager, binary=binary, command_runner=run, process_launcher=launch)
                target = self.target()
                runtime._process = previous
                runtime._config = target.config
                runtime._config_text = target.text
                runtime._last_input_latency_ms = 750
                runtime.configure_latency_tracking(input_latency_provider=lambda: 750)
                runtime._wait_for_ports = complete
                runtime._control = control
                runtime._control_unlocked = control
                try:
                    await runtime.sync_rendered(target)
                    self.assertIsNot(runtime._process, previous)
                    self.assertEqual([command for command in commands if command.startswith('input latency ')],
                                     ['input latency 750'])
                finally:
                    await runtime.stop()
        asyncio.run(exercise())

    def test_paused_source_ingress_leaves_latency_pending_but_output_settling_fails(self):
        from audio.src_latency import SrcGraphSettling, graph_src_latency
        from test_av_sync_src import node, link

        async def exercise(hot):
            with tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / "fxroute-dsp"
                binary.touch()
                applied = []
                states = {"ingress": "init", "output": "active"}

                async def run(command):
                    return CommandResult(0)

                async def control(command, **_kwargs):
                    if command.startswith("input latency "):
                        applied.append(float(command.split()[-1]))
                    if command == "latency get":
                        return json.dumps({"ready": True, "rate": 48000, "input": 0, "target": 0, "minimum": 0,
                                           "maximum": 0, "common": 0, "actual": 0, "outputs": [
                                               {key: 0 for key in ("dsp", "intentional", "align", "balance",
                                                                   "sync", "total")}] * 2})
                    return "0" if command == "effects bypass get" else "ok"

                async def complete(*_args, **_kwargs):
                    pass

                async def launch(_command):
                    return FakeProcess()

                runtime = DSPRuntime(self.manager, binary=binary, command_runner=run, process_launcher=launch)
                runtime._process = FakeProcess()
                runtime._control_socket = unittest.mock.Mock()
                runtime._config = self.config(output_key="new" if hot else "vanished")
                runtime._stop_orphan_helpers = complete
                runtime._wait_for_ports = complete
                runtime._remove_direct_source_links = complete
                runtime._reconcile_ingress_links = complete
                runtime._reconcile_output_links = complete
                runtime._control = control
                runtime._control_unlocked = control

                # MPV's Surround ingress: new links to a paused stream stay in init.
                def latency():
                    return graph_src_latency([
                        node(5, 'mpv', 'Stream/Output/Audio'), node(3, 'fxroute_dsp'),
                        node(4, 'new', 'Audio/Sink', 250000000),
                        link(5, 3, states["ingress"]), link(3, 4, states["output"])], ingress_nodes=('mpv',))

                runtime.configure_latency_tracking(input_latency_provider=latency)
                try:
                    await runtime.sync_rendered(self.target(self.config(output_key="new")))
                    self.assertEqual(runtime._config.output_key, "new")
                    self.assertEqual(applied, [])
                    self.assertIsNone(runtime._last_input_latency_ms)
                    # Unknown, never a guessed zero: the readback is not ready.
                    self.assertEqual(await runtime.read_latency(), {"ready": False})
                    states["ingress"] = "active"
                    await runtime.update_input_latency()
                    self.assertEqual(applied, [250])
                    self.assertTrue((await runtime.read_latency())["ready"])
                    states["output"] = "init"
                    with self.assertRaises(SrcGraphSettling):
                        await runtime.update_input_latency()
                finally:
                    await runtime.stop()

        for hot in (False, True):
            with self.subTest(hot=hot):
                asyncio.run(exercise(hot))

    def test_committed_graph_survives_unavailable_src_latency_and_recovers(self):
        from audio.src_latency import SrcGraphSettling

        async def exercise(kind, failure, transient):
            with tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / 'fxroute-dsp'
                binary.touch()
                links, commands, launches = set(), [], []
                unavailable = True

                async def run(args):
                    if args == ('pw-link', '-l'):
                        return CommandResult(0, '\n'.join(f'{a} -> {b}' for a, b in links))
                    if args[0] == 'pw-link':
                        if args[1] == '-d':
                            links.discard(tuple(args[2:]))
                        else:
                            links.add(tuple(args[1:]))
                    return CommandResult(0)

                async def launch(args):
                    process = FakeProcess()
                    launches.append(process)
                    return process

                async def control(command, **kwargs):
                    commands.append(command)
                    return '0' if command == 'effects bypass get' else 'ok\n'

                async def complete(*args):
                    pass

                def latency():
                    if unavailable:
                        raise failure
                    return 250

                runtime = DSPRuntime(self.manager, binary=binary, command_runner=run,
                                     process_launcher=launch)
                target = self.target()
                original = FakeProcess() if kind != 'cold' else None
                runtime._process = original
                runtime._config = target.config if original else None
                runtime._control_socket = unittest.mock.Mock() if original else None
                runtime._config_text = target.text if kind == 'live' else None
                runtime._control = runtime._control_unlocked = control
                runtime._stop_orphan_helpers = runtime._wait_for_ports = complete
                runtime.configure_latency_tracking(input_latency_provider=latency)
                try:
                    if not transient:
                        with self.assertRaisesRegex(RuntimeError, str(failure)):
                            await runtime.sync_rendered(target)
                        return
                    await runtime.sync_rendered(target)
                    self.assertTrue(runtime.snapshot()['active'])
                    self.assertTrue(await runtime.verify())
                    self.assertEqual(len(launches), int(kind == 'cold'))
                    if original:
                        self.assertIs(runtime._process, original)
                        self.assertFalse(original.terminated)
                    self.assertEqual(await runtime.read_latency(), {'ready': False})
                    self.assertFalse(any(c.startswith('input latency ') for c in commands))
                    unavailable = False
                    await runtime.update_input_latency()
                    self.assertEqual(runtime._last_input_latency_ms, 250)
                    self.assertIn('input latency 250', commands)
                finally:
                    await runtime.stop()

        from audio.src_latency import graph_src_latency
        from test_av_sync_src import graph
        try:
            graph_src_latency(graph()[:8])
        except RuntimeError as exc:
            missing_path = exc
        for kind in ('cold', 'swap', 'live'):
            for failure, transient in ((SrcGraphSettling('Output is negotiating', path='output'), True),
                                       (missing_path, True),
                                       (RuntimeError('SRC latency adapter is unavailable'), False),
                                       (RuntimeError('SRC latency adapter evidence is unavailable'), False),
                                       (RuntimeError('Linked audio paths have different SRC latency'), False)):
                with self.subTest(kind=kind, failure=failure):
                    asyncio.run(exercise(kind, failure, transient))

    def test_pending_latency_preserves_health_with_backoff_and_recovers(self):
        from audio.src_latency import SrcLatencyReader
        from test_av_sync_src import graph
        clock, reads, timed_out = [0.0], [], []
        runtime = DSPRuntime(self.manager)
        runtime._process = FakeProcess()
        runtime._config = self.config()
        runtime._links = [PipeWireLink('fxroute_dsp:output_1', 'hw:playback_FL')]
        reader = SrcLatencyReader()
        def dump():
            reads.append(clock[0])
            return graph() if clock[0] >= 14 else graph()[:8]
        async def control(command, **kwargs):
            if command == 'latency get':
                return json.dumps({'ready': True, 'rate': 48000, 'input': 36000, 'target': 0,
                    'minimum': 0, 'maximum': 0, 'common': 0, 'actual': 0,
                    'outputs': [{key: 0 for key in ('dsp', 'intentional', 'align', 'balance', 'sync', 'total')}] * 2})
            return 'ok'
        async def sleep(delay):
            clock[0] += delay
            if 10 <= clock[0] < 14:
                timed_out.append(runtime.snapshot())
            if clock[0] > 20:
                raise AssertionError('Latency watcher never recovered')
        async def ready(value):
            raise asyncio.CancelledError
        runtime._control_unlocked = control
        runtime.configure_latency_tracking(input_latency_provider=reader.read)
        async def exercise():
            await runtime.update_input_latency(defer_unavailable=True)
            with self.assertRaises(asyncio.CancelledError):
                await runtime.latency_watch_loop(on_ready=ready)
        with unittest.mock.patch('dsp.runtime.time.monotonic', side_effect=lambda: clock[0]), \
                unittest.mock.patch('dsp.runtime.asyncio.sleep', side_effect=sleep), \
                unittest.mock.patch.object(reader, '_dump', side_effect=dump), \
                self.assertLogs('dsp.runtime', level='WARNING') as logs:
            asyncio.run(exercise())
        self.assertLessEqual(len(reads), 16, 'Unavailable evidence must not run pw-dump at 10 Hz')
        self.assertLessEqual(len(logs.output), 4, 'Unavailable evidence warnings must be throttled')
        self.assertTrue(timed_out)
        self.assertTrue(all(row['active'] and row['last_error'] is None
                            and row['ingress_latency_pending'] and not row['av_sync'] for row in timed_out))
        self.assertFalse(runtime.snapshot().get('ingress_latency_pending'))
        self.assertIsNone(runtime.snapshot()['last_error'])
        self.assertTrue(runtime.snapshot()['active'])
        self.assertEqual(runtime._last_input_latency_ms, 750)

    def test_fatal_src_error_survives_pending_until_acknowledged_recovery(self):
        from audio.src_latency import SrcPathUnavailable
        async def exercise():
            runtime = DSPRuntime(self.manager)
            runtime._process = FakeProcess()
            runtime._config = self.config()
            runtime._links = [PipeWireLink('fxroute_dsp:output_1', 'hw:playback_FL')]
            fault = RuntimeError('SRC latency adapter is unavailable')
            provider = unittest.mock.Mock(side_effect=[
                fault, SrcPathUnavailable('Ingress missing', path='ingress'),
                SrcPathUnavailable('Output missing', path='output'), 125, 125])
            runtime.configure_latency_tracking(input_latency_provider=provider)
            runtime._control_unlocked = unittest.mock.AsyncMock(side_effect=[RuntimeError('No acknowledgement'), 'ok'])
            with unittest.mock.patch('dsp.runtime.time.monotonic', return_value=5):
                with self.assertRaisesRegex(RuntimeError, 'adapter is unavailable'):
                    await runtime.update_input_latency(defer_unavailable=True)
            for now in (6, 20):
                with unittest.mock.patch('dsp.runtime.time.monotonic', return_value=now):
                    await runtime.update_input_latency(defer_unavailable=True)
                self.assertFalse(runtime.snapshot()['active'])
                self.assertEqual(runtime.snapshot()['last_error'], str(fault))
                self.assertTrue(runtime.snapshot()['ingress_latency_pending'])
                self.assertEqual(await runtime.read_latency(), {'ready': False})
            with self.assertRaisesRegex(RuntimeError, 'No acknowledgement'):
                await runtime.update_input_latency()
            self.assertFalse(runtime.snapshot()['active'])
            self.assertEqual(runtime.snapshot()['last_error'], str(fault))
            await runtime.update_input_latency()
            self.assertTrue(runtime.snapshot()['active'])
            self.assertIsNone(runtime.snapshot()['last_error'])
            self.assertFalse(runtime.snapshot()['ingress_latency_pending'])
            self.assertEqual(runtime._last_input_latency_ms, 125)
        asyncio.run(exercise())

    def test_late_missing_latency_does_not_poison_a_new_graph(self):
        from audio.src_latency import SrcGraphSettling
        async def exercise():
            runtime = DSPRuntime(self.manager)
            runtime._process = FakeProcess()
            runtime._config = self.config()
            entered, release = asyncio.Event(), asyncio.Event()
            async def latency():
                entered.set()
                await release.wait()
                raise SrcGraphSettling('Old output negotiating', path='output')
            runtime.configure_latency_tracking(input_latency_provider=latency)
            task = asyncio.create_task(runtime.update_input_latency(defer_unavailable=True))
            await entered.wait()
            runtime._config = self.config(output_key='new')
            release.set()
            await task
            self.assertFalse(runtime._ingress_latency_pending)
        asyncio.run(exercise())

    def test_rejected_preflight_preserves_pending_latency_deadline(self):
        from audio.src_latency import SrcGraphSettling
        clock = [0.0]
        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / 'fxroute-dsp'
                binary.touch()
                async def run(args):
                    return CommandResult(1, '', 'Invalid candidate')
                runtime = DSPRuntime(self.manager, binary=binary, command_runner=run)
                runtime._process = FakeProcess()
                target = self.target()
                runtime._config = target.config
                runtime._config_text = target.text
                def unavailable():
                    raise SrcGraphSettling('Output negotiating', path='output')
                runtime.configure_latency_tracking(input_latency_provider=unavailable)
                await runtime.update_input_latency(defer_unavailable=True)
                with self.assertRaisesRegex(RuntimeError, 'Invalid candidate'):
                    await runtime.sync_rendered(target)
                clock[0] = 13
                await runtime.update_input_latency(defer_unavailable=runtime._ingress_latency_pending)
                snapshot = runtime.snapshot()
                self.assertTrue(snapshot['ingress_latency_pending'])
                self.assertEqual(snapshot['ingress_latency_pending_seconds'], 13)
                self.assertIsNone(snapshot['last_error'])
        with unittest.mock.patch('dsp.runtime.time.monotonic', side_effect=lambda: clock[0]):
            asyncio.run(exercise())

    def test_hot_update_fallback_excludes_fatal_latency_evidence(self):
        events = []

        async def exercise(fail_stage):
            with tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / "fxroute-dsp"
                binary.touch()
                runtime = DSPRuntime(self.manager, binary=binary)
                runtime._process = FakeProcess()
                runtime._control_socket = unittest.mock.Mock()
                runtime._config = self.config()
                runtime._links = []

                async def run(command):
                    events.append(("run", tuple(command)))
                    return CommandResult(0, "")

                async def launch(command):
                    events.append(("launch", tuple(command), Path(command[1]).exists()))
                    self.assertTrue(Path(command[1]).is_file(), 'Fallback config must still exist at launch')
                    return FakeProcess()

                async def control(command, **_kwargs):
                    events.append(("control", command))
                    return "0\n" if command == "effects bypass get" else "ok\n"

                runtime._run = run
                runtime._launch = launch
                runtime._control = control
                runtime._control_unlocked = control
                runtime._stop_orphan_helpers = lambda: complete()
                runtime._wait_for_ports = lambda _config: complete()
                runtime._remove_direct_source_links = lambda: complete()
                reconcile = runtime._reconcile_output_links
                failed = False

                async def fail_once(config):
                    nonlocal failed
                    if not failed and fail_stage == 'links':
                        failed = True
                        raise RuntimeError("link failed")
                    await reconcile(config)

                def latency():
                    nonlocal failed
                    if not failed:
                        failed = True
                        raise RuntimeError('SRC adapter is unavailable')
                    return 250

                if fail_stage == 'latency':
                    runtime.configure_latency_tracking(input_latency_provider=latency)

                runtime._reconcile_output_links = fail_once
                if fail_stage == 'latency':
                    with self.assertRaisesRegex(RuntimeError, 'SRC adapter is unavailable'):
                        await runtime.sync_rendered(self.target())
                else:
                    await runtime.sync_rendered(self.target())
                await runtime.stop()

        async def complete():
            return None

        for fail_stage in ('links', 'latency'):
            with self.subTest(fail_stage=fail_stage):
                asyncio.run(exercise(fail_stage))
        launch_events = [event for event in events if event[0] == "launch"]
        self.assertEqual(len(launch_events), 1)
        self.assertTrue(all(event[2] for event in launch_events))

    def test_runtime_snapshot_reports_actual_layout_and_effect_bypass(self):
        runtime = DSPRuntime(self.manager)
        runtime._config = self.config("stereo-sub")
        runtime._effect_bypass = True
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot["config"]["sample_rate"], 48000)
        self.assertEqual(snapshot["config"]["output_mode"], "stereo-sub")
        self.assertEqual(len(snapshot["config"]["layout"]), 4)
        self.assertTrue(snapshot["effect_bypass"])

    def test_effect_bypass_restore_uses_engine_readback_not_shadow_state(self):
        commands = []

        async def exercise():
            runtime = DSPRuntime(self.manager)
            runtime._effect_bypass = False
            async def control(command, *, reply):
                commands.append(command)
                return "1\n" if command == "effects bypass get" else "ok\n"
            runtime._control_unlocked = control
            previous = await runtime.set_effect_bypass(True)
            self.assertTrue(previous)

        asyncio.run(exercise())
        self.assertEqual(commands, ["effects bypass get", "effects bypass 1"])

    def test_control_requests_are_serialized(self):
        active = 0
        maximum = 0

        async def exercise():
            runtime = DSPRuntime(self.manager)

            async def control(_command, *, reply):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0.01)
                active -= 1
                return "ok\n" if reply else ""

            runtime._control_unlocked = control
            await asyncio.gather(
                runtime._control("first", reply=True),
                runtime._control("second", reply=True),
            )

        asyncio.run(exercise())
        self.assertEqual(maximum, 1)

    def test_cancelled_raw_scope_entry_restores_bypass_before_unlocking(self):
        state = False
        bypass_started = asyncio.Event()
        release_bypass = asyncio.Event()

        async def exercise():
            nonlocal state
            runtime = DSPRuntime(self.manager)

            async def control(command, *, reply):
                nonlocal state
                if command == "effects bypass get":
                    return f"{int(state)}\n"
                if command == "effects bypass 1":
                    state = True
                    bypass_started.set()
                    await release_bypass.wait()
                elif command == "effects bypass 0":
                    state = False
                return "ok\n" if reply else ""

            runtime._control_unlocked = control
            task = asyncio.create_task(runtime.enter_raw_measurement())
            await bypass_started.wait()
            task.cancel()
            release_bypass.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(state)
            self.assertFalse(runtime._measurement_scope_lock.locked())

        asyncio.run(exercise())

    def test_repeated_cancel_keeps_raw_scope_until_restore_completes(self):
        restore_started = asyncio.Event()
        restore_may_finish = asyncio.Event()
        restore_finished = asyncio.Event()

        async def exercise():
            runtime = DSPRuntime(self.manager)

            async def fake_set_bypass(enabled):
                restore_started.set()
                await restore_may_finish.wait()
                restore_finished.set()
                return False

            runtime.set_effect_bypass = fake_set_bypass
            await runtime._measurement_scope_lock.acquire()
            task = asyncio.create_task(runtime.exit_raw_measurement(False))
            await restore_started.wait()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0.05)
            self.assertTrue(runtime._measurement_scope_lock.locked())
            contender = asyncio.create_task(runtime._measurement_scope_lock.acquire())
            await asyncio.sleep(0.05)
            # No second measurement path may take the scope while the old
            # restore is still in flight.
            self.assertFalse(contender.done())
            contender.cancel()
            try:
                await contender
            except asyncio.CancelledError:
                pass
            task.cancel()
            await asyncio.sleep(0.05)
            self.assertTrue(runtime._measurement_scope_lock.locked())
            restore_may_finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            self.assertTrue(restore_finished.is_set())
            self.assertFalse(runtime._measurement_scope_lock.locked())

        asyncio.run(exercise())

    def test_failed_raw_scope_restore_releases_scope_and_raises(self):
        async def exercise():
            runtime = DSPRuntime(self.manager)

            async def failing_set_bypass(enabled):
                raise RuntimeError("engine unreachable")

            runtime.set_effect_bypass = failing_set_bypass
            await runtime._measurement_scope_lock.acquire()
            with self.assertRaises(RuntimeError):
                await runtime.exit_raw_measurement(False)
            self.assertFalse(runtime._measurement_scope_lock.locked())

        asyncio.run(exercise())

    def guarded_runtime(self, events, *, control_socket=True):
        runtime = DSPRuntime(self.manager)
        if control_socket:
            runtime._control_socket = unittest.mock.Mock()

        async def record(event):
            events.append(event)

        async def operating():
            return 0.0

        runtime._operating_output_gain_db = operating
        runtime.set_output_gain_db = lambda value: record(("gain", value))
        runtime.ramp_output_gain_db = lambda start, target, **_kwargs: record(("ramp", start, target))
        runtime._settle_output_gain_db = lambda target: record(("settle", target))
        return runtime

    def test_guarded_rebuild_starts_candidate_at_guard_and_ramps(self):
        events = []

        async def exercise():
            runtime = self.guarded_runtime(events)

            async def run_sync(_config, _text, **kwargs):
                events.append(("sync", kwargs["initial_output_gain_db"]))
            runtime._run_sync = run_sync
            await runtime.guarded_rebuild_rendered(
                self.target(), previous=self.target(), guard_db=-18,
                apply_candidate=lambda: events.append(("apply", "new")),
                apply_previous=lambda: events.append(("apply", "old")),
                settle_seconds=0)

        asyncio.run(exercise())
        self.assertEqual(events, [
            ("gain", -18), ("sync", -18), ("ramp", -18, 0.0), ("settle", 0.0), ("apply", "new"),
        ])

    def test_guarded_rebuild_on_cold_runtime_pins_guard_through_sync(self):
        # The first native start must behave like every later rebuild: the
        # guard cannot be written to a nonexistent engine, so it must reach
        # the engine through the sync initial gain and the ramp.  Without
        # this the first mode switch started the engine at 0 dB with no ramp.
        events = []

        async def exercise():
            runtime = self.guarded_runtime(events, control_socket=False)

            async def run_sync(_config, _text, **kwargs):
                events.append(("sync", kwargs["initial_output_gain_db"]))
            runtime._run_sync = run_sync
            await runtime.guarded_rebuild_rendered(
                self.target(), previous=self.target(), guard_db=-18,
                apply_candidate=lambda: events.append(("apply", "new")),
                apply_previous=lambda: events.append(("apply", "old")),
                settle_seconds=0)

        asyncio.run(exercise())
        self.assertEqual(events, [
            ("sync", -18), ("ramp", -18, 0.0), ("settle", 0.0), ("apply", "new"),
        ])

    def test_guarded_rebuild_rolls_back_previous_state_on_failure(self):
        events = []

        async def exercise():
            runtime = self.guarded_runtime(events)
            sync_count = 0

            async def run_sync(_config, _text, **kwargs):
                nonlocal sync_count
                sync_count += 1
                events.append(("sync", sync_count, kwargs["initial_output_gain_db"]))
                if sync_count == 1:
                    raise RuntimeError("candidate failed")
            runtime._run_sync = run_sync
            with self.assertRaisesRegex(RuntimeError, "candidate failed"):
                await runtime.guarded_rebuild_rendered(
                    self.target(), previous=self.target(), guard_db=-24,
                    apply_candidate=lambda: events.append(("apply", "new")),
                    apply_previous=lambda: events.append(("apply", "old")),
                    settle_seconds=0)

        asyncio.run(exercise())
        self.assertEqual(events, [
            ("gain", -24), ("sync", 1, -24),
            ("apply", "old"), ("sync", 2, -24), ("ramp", -24, 0.0), ("settle", 0.0),
        ])

    def test_guarded_rebuild_preserves_original_error_when_rollback_fails(self):
        events = []

        async def exercise():
            runtime = self.guarded_runtime(events)

            async def run_sync(_config, _text, **_kwargs):
                raise RuntimeError("candidate failed")
            runtime._run_sync = run_sync
            with self.assertRaisesRegex(RuntimeError, "candidate failed"):
                await runtime.guarded_rebuild_rendered(
                    self.target(), previous=self.target(), guard_db=-18,
                    apply_candidate=lambda: None,
                    apply_previous=lambda: (_ for _ in ()).throw(RuntimeError("rollback failed")),
                    settle_seconds=0)

        asyncio.run(exercise())

    def test_reclean_removes_direct_player_hardware_links(self):
        commands = []
        live = "\n".join((
            "mpv:output_FL\n  |-> hw:playback_FL",
            "spotify:output_RR\n  |-> hw:playback_RR",
        ))

        async def run(command):
            commands.append(tuple(command))
            return CommandResult(0, live if tuple(command) == ("pw-link", "-l") else "")

        runtime = DSPRuntime(self.manager, command_runner=run)
        runtime._config = self.config()
        asyncio.run(runtime._remove_direct_source_links())
        self.assertIn(("pw-link", "-d", "mpv:output_FL", "hw:playback_FL"), commands)
        self.assertIn(("pw-link", "-d", "spotify:output_RR", "hw:playback_RR"), commands)
        # Only edges that really exist are disconnected: probing the whole
        # candidate matrix would cost one failing `pw-link -d` per combination
        # (2 sources x ports x channels) on a healthy graph.
        self.assertEqual(
            sorted(command for command in commands if command[:2] == ("pw-link", "-d")),
            [
                ("pw-link", "-d", "mpv:output_FL", "hw:playback_FL"),
                ("pw-link", "-d", "spotify:output_RR", "hw:playback_RR"),
            ],
        )

    def test_reclean_removes_an_existing_direct_link_on_a_multichannel_device(self):
        commands = []
        live = "\n".join(
            f"mpv:output_FR\n  |-> hw:playback_AUX{index}" for index in range(18)
        )

        async def run(command):
            commands.append(tuple(command))
            return CommandResult(0, live if tuple(command) == ("pw-link", "-l") else "")

        runtime = DSPRuntime(self.manager, command_runner=run)
        runtime._config = DSPRuntimeConfig(
            "stereo-sub", "hw", 48000,
            tuple(f"playback_AUX{index}" for index in range(18)), (),
        )
        asyncio.run(runtime._remove_direct_source_links())
        self.assertEqual(
            sorted(command for command in commands if command[:2] == ("pw-link", "-d")),
            sorted(("pw-link", "-d", "mpv:output_FR", f"hw:playback_AUX{index}") for index in range(18)),
        )

    def test_reclean_reconciles_stable_native_sub_output_links(self):
        commands = []

        async def run(command):
            commands.append(tuple(command))
            return CommandResult(0, "")

        runtime = DSPRuntime(self.manager, command_runner=run)
        runtime._config = self.config("stereo-sub")
        runtime._links = [
            PipeWireLink("fxroute_dsp:output_1", "hw:playback_FL"),
            PipeWireLink("fxroute_dsp:output_2", "hw:playback_FR"),
        ]
        asyncio.run(runtime.reclean_direct_dsp_links())
        self.assertIn(("pw-link", "fxroute_dsp:output_3", "hw:playback_RL"), commands)
        self.assertIn(("pw-link", "fxroute_dsp:output_4", "hw:playback_RR"), commands)

    def test_reclean_waits_for_runtime_lock(self):
        events = []

        async def run(command):
            events.append(tuple(command))
            return CommandResult(0, "")

        async def exercise():
            runtime = DSPRuntime(self.manager, command_runner=run)
            runtime._config = self.config()
            await runtime._lock.acquire()
            task = asyncio.create_task(runtime._reclean_guarded())
            await asyncio.sleep(0)
            self.assertEqual(events, [])
            runtime._lock.release()
            await task

        asyncio.run(exercise())

    def test_preflight_failure_preserves_running_runtime_and_removes_candidates(self):
        preflight_paths = []

        async def run(command):
            if command[0].endswith("fxroute-dsp-offline"):
                preflight_paths.extend(Path(value) for value in command[1:])
                return CommandResult(1, stderr="plugin failed")
            return CommandResult(0)

        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / "fxroute-dsp"
                binary.touch()
                old_config = Path(directory) / "old.conf"
                old_config.write_text("old")
                old_process = FakeProcess()
                runtime = DSPRuntime(self.manager, binary=binary, command_runner=run)
                runtime._process = old_process
                runtime._config = self.config()
                runtime._config_path = old_config
                runtime._links = [PipeWireLink("old-source", "old-target")]
                with self.assertRaisesRegex(RuntimeError, "plugin failed"):
                    await runtime.sync_rendered(self.target())
                self.assertIs(runtime._process, old_process)
                self.assertFalse(old_process.terminated)
                self.assertEqual(runtime._config_path, old_config)
                self.assertTrue(old_config.exists())
                self.assertEqual(runtime._links, [PipeWireLink("old-source", "old-target")])

        asyncio.run(exercise())
        self.assertEqual(len(preflight_paths), 3)
        self.assertTrue(all(not path.exists() for path in preflight_paths))

    def test_launch_failure_cleans_candidate_resources_and_sets_error(self):
        candidate_paths = []

        async def run(command):
            if command[0].endswith("fxroute-dsp-offline"):
                candidate_paths.extend(Path(value) for value in command[1:])
            return CommandResult(0)

        async def launch(command):
            candidate_paths.extend((Path(command[1]), Path(command[2])))
            raise RuntimeError("launch failed")

        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / "fxroute-dsp"
                binary.touch()
                old_process = FakeProcess()
                runtime = DSPRuntime(self.manager, binary=binary, command_runner=run,
                                     process_launcher=launch)
                runtime._process = old_process
                runtime._config = self.config()
                runtime._links = [PipeWireLink("old-source", "old-target")]
                with self.assertRaisesRegex(RuntimeError, "launch failed"):
                    await runtime.sync_rendered(self.target())
                self.assertTrue(old_process.terminated)
                self.assertIsNone(runtime._process)
                self.assertIsNone(runtime._config_path)
                self.assertIsNone(runtime._control_socket)
                self.assertIsNone(runtime._control_path)
                self.assertIsNone(runtime._control_client_path)
                self.assertEqual(runtime._links, [])
                self.assertEqual(runtime.snapshot()["last_error"], "launch failed")

        asyncio.run(exercise())
        self.assertTrue(candidate_paths)
        self.assertTrue(all(not path.exists() for path in candidate_paths))



class DSPRuntimeLifecycleTests(unittest.TestCase):
    """Process lifecycle: bounded commands, stderr drain, orphan cleanup."""

    def setUp(self):
        self.manager = DSPManager(home=Path(tempfile.mkdtemp()))
        self.manager.save_global_extras({"limiter": {"enabled": False}})

    def test_run_command_timeout_terminates_kills_and_reaps_child(self):
        import dsp.runtime as runtime_module

        original_timeout = runtime_module.RUNTIME_COMMAND_TIMEOUT_SECONDS
        runtime_module.RUNTIME_COMMAND_TIMEOUT_SECONDS = 0.4
        try:
            result = asyncio.run(
                runtime_module.DSPRuntime._run_command(
                    [sys.executable, "-c", "import time; time.sleep(30)"]))
        finally:
            runtime_module.RUNTIME_COMMAND_TIMEOUT_SECONDS = original_timeout
        return result

        result = run()
        self.assertEqual(result.returncode, RUNTIME_COMMAND_TIMEOUT_RETURNCODE)
        self.assertIn("timed out", result.stderr)

    def test_run_command_caller_cancellation_reaps_child(self):
        import unittest.mock as mock
        created = []

        original = asyncio.create_subprocess_exec

        async def capturing_exec(*args, **kwargs):
            process = await original(*args, **kwargs)
            created.append(process)
            return process

        async def exercise():
            with mock.patch.object(asyncio, "create_subprocess_exec", new=capturing_exec):
                task = asyncio.create_task(
                    DSPRuntime._run_command(
                        [sys.executable, "-c", "import time; time.sleep(30)"]))
                await asyncio.sleep(0.2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(exercise())
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].returncode, "cancelled command child was not reaped")

    def test_orphan_cleanup_terminates_stale_engines_and_spares_own(self):
        commands = []

        async def run(command):
            commands.append(tuple(command))
            if command == ("pgrep", "-f", pattern):
                return CommandResult(0, "123\n456\n")
            return CommandResult(0, "")

        async def exercise():
            runtime = DSPRuntime(self.manager, binary=Path("/usr/bin/fxroute-dsp"),
                                 command_runner=run)
            own = FakeProcess()
            own.pid = 123
            runtime._process = own
            await runtime._stop_orphan_helpers()

        pattern = r"/usr/bin/fxroute\-dsp\s"
        # The assertion is the signal sequence, not the real 0.5 s grace
        # window; shrink it so the test does not sleep twice per run.
        with unittest.mock.patch(
            "dsp.runtime.RUNTIME_ORPHAN_KILL_GRACE_SECONDS", 0.01
        ):
            asyncio.run(exercise())
        self.assertEqual(commands[0], ("pgrep", "-f", pattern))
        self.assertTrue(all("123" not in str(command) for command in commands[1:]),
                        "own process must never be signalled")

    def test_orphan_cleanup_skips_when_no_orphans(self):
        commands = []

        async def run(command):
            commands.append(tuple(command))
            return CommandResult(0, "")

        async def exercise():
            runtime = DSPRuntime(self.manager, binary=Path("/usr/bin/fxroute-dsp"),
                                 command_runner=run)
            await runtime._stop_orphan_helpers()

        asyncio.run(exercise())
        self.assertEqual(len(commands), 1)

    def test_orphan_cleanup_kills_survivors_of_sigterm(self):
        commands = []

        async def run(command):
            commands.append(tuple(command))
            if len(commands) == 1:
                return CommandResult(0, "999\n")
            return CommandResult(0, "999\n")

        async def exercise():
            runtime = DSPRuntime(self.manager, binary=Path("/usr/bin/fxroute-dsp"),
                                 command_runner=run)
            await runtime._stop_orphan_helpers()

        # The assertion is the terminate-then-kill sequence, not the real
        # 0.5 s grace window; shrink it so the test does not sleep per run.
        with unittest.mock.patch(
            "dsp.runtime.RUNTIME_ORPHAN_KILL_GRACE_SECONDS", 0.01
        ):
            asyncio.run(exercise())
        self.assertEqual([command[0] for command in commands], ["pgrep", "pgrep"])

    def test_engine_stderr_drain_keeps_bounded_tail(self):
        async def exercise():
            runtime = DSPRuntime(self.manager)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-c",
                "import sys; sys.stderr.write('x' * 200000); sys.stderr.flush()")
            runtime._process = process
            runtime._start_stderr_drain(process)
            await asyncio.wait_for(process.wait(), 10)
            for _ in range(100):
                if not runtime._stderr_drain_task or runtime._stderr_drain_task.done():
                    break
                await asyncio.sleep(0.05)
            await runtime._stop_stderr_drain()
            tail = runtime.stderr_tail()
            self.assertTrue(tail)
            self.assertLessEqual(len(tail.encode("utf-8")), 64 * 1024 + 4096)
            self.assertIsNone(runtime._stderr_drain_task)

    def test_engine_stderr_drain_is_stopped_by_stop(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / "fxroute-dsp"
                binary.touch()
                runtime = DSPRuntime(self.manager, binary=binary)
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-c",
                    "import sys; sys.stderr.write('y' * 4096); sys.stderr.flush(); "
                    "import time; time.sleep(30)")
                runtime._process = process
                runtime._start_stderr_drain(process)
                await asyncio.sleep(0.2)
                self.assertIsNotNone(runtime._stderr_drain_task)
                await runtime.stop()
                self.assertIsNone(runtime._stderr_drain_task)
                self.assertIsNone(runtime._process)

    def test_stderr_drain_keeps_capturing_while_process_terminates(self):
        async def exercise():
            runtime = DSPRuntime(self.manager)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-c",
                "import signal, sys, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "sys.stderr.write('start\n'); sys.stderr.flush(); "
                "time.sleep(1.0); "
                "sys.stderr.write('during-shutdown\n'); sys.stderr.flush(); "
                "time.sleep(1.0)")
            runtime._process = process
            runtime._start_stderr_drain(process)
            await asyncio.sleep(0.2)
            await runtime.stop()
            self.assertIsNone(runtime._stderr_drain_task)
            self.assertIsNone(runtime._process)
            self.assertIsNotNone(process.returncode, "engine was not reaped")
            tail = runtime.stderr_tail()
            self.assertIn("start", tail)
            self.assertIn("during-shutdown", tail)

    def test_stop_after_launch_failure_has_no_stderr_task(self):
        async def exercise():
            runtime = DSPRuntime(self.manager)
            self.assertIsNone(runtime._stderr_drain_task)
            await runtime._stop_stderr_drain()
            self.assertIsNone(runtime._stderr_drain_task)

    def test_orphan_cleanup_spares_own_pid_and_signals_only_others(self):
        signalled = []
        pgrep_calls = []

        async def run(command):
            if command == ("pgrep", "-f", pattern):
                pgrep_calls.append(command)
                if len(pgrep_calls) == 1:
                    return CommandResult(0, "123\n456\n")
                return CommandResult(0, "")
            return CommandResult(0, "")

        original_kill = os.kill

        def recording_kill(pid, sig):
            signalled.append((pid, sig))
            if pid == 456:
                raise ProcessLookupError(pid)

        async def exercise():
            runtime = DSPRuntime(self.manager, binary=Path("/usr/bin/fxroute-dsp"),
                                 command_runner=run)
            own = FakeProcess()
            own.pid = 123
            runtime._process = own
            with unittest.mock.patch.object(os, "kill", new=recording_kill):
                await runtime._stop_orphan_helpers()

        pattern = r"/usr/bin/fxroute\-dsp\s"
        asyncio.run(exercise())
        self.assertEqual(signalled, [(456, signal.SIGTERM)])



class VerifiedShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_disconnect_and_unreaped_helper_keep_process_identity(self):
        class StubbornProcess:
            pid = 4321
            returncode = None

            def terminate(self):
                pass

            def kill(self):
                pass

            async def wait(self):
                raise asyncio.TimeoutError()

        async def run(args):
            return CommandResult(1, "", "disconnect failed")

        runtime = DSPRuntime(object(), command_runner=run)
        child = runtime._process = StubbornProcess()
        links = runtime._links = [PipeWireLink("fxroute-dsp:output_1", "hw:playback_FL")]
        stop = getattr(runtime, "stop_verified", runtime.stop)
        with self.assertRaisesRegex(RuntimeError, "exit"):
            await stop()
        self.assertIs(runtime._process, child)
        self.assertEqual(runtime._links, links)
        self.assertEqual(runtime.snapshot()["helper_pid"], 4321)

    async def test_verified_exit_is_safe_even_if_link_disconnect_failed(self):
        async def run(args):
            return CommandResult(1, "", "disconnect failed")

        runtime = DSPRuntime(object(), command_runner=run)
        child = runtime._process = FakeProcess()
        runtime._links = [PipeWireLink("fxroute-dsp:output_1", "hw:playback_FL")]
        stop = getattr(runtime, "stop_verified", runtime.stop)
        await stop()
        self.assertEqual(child.returncode, 0)
        self.assertIsNone(runtime._process)
        self.assertFalse(runtime.snapshot()["active"])
        self.assertIsNone(runtime.snapshot()["helper_pid"])


class ContainsLinkTests(unittest.TestCase):
    """Link recognition must be independent of link ordering within a port."""

    def test_link_is_recognized_as_first_entry_under_target(self):
        text = (
            "\tfxroute_dsp_sink:playback_FL\n"
            "  |<- mpv:output_FL\n"
            "\tfxroute_dsp_sink:playback_FR\n"
            "  |<- mpv:output_FR\n"
        )
        self.assertTrue(_contains_link(text, "mpv:output_FL", "fxroute_dsp_sink:playback_FL"))
        self.assertTrue(_contains_link(text, "mpv:output_FR", "fxroute_dsp_sink:playback_FR"))

    def test_link_is_recognized_behind_other_links_to_same_target(self):
        # Spotify is connected to the sink before mpv; the old fixed-pattern
        # check could not see the mpv link behind the interleaved line.
        text = (
            "\tfxroute_dsp_sink:playback_FL\n"
            "  |<- spotify:output_FL\n"
            "  |<- mpv:output_FL\n"
            "\tfxroute_dsp_sink:playback_FR\n"
            "  |<- spotify:output_FR\n"
            "  |<- mpv:output_FR\n"
        )
        self.assertTrue(_contains_link(text, "mpv:output_FL", "fxroute_dsp_sink:playback_FL"))
        self.assertTrue(_contains_link(text, "mpv:output_FR", "fxroute_dsp_sink:playback_FR"))

    def test_link_is_recognized_via_source_arrow_form(self):
        text = (
            "\tmpv:output_FL\n"
            "  |-> fxroute_dsp_sink:playback_FL\n"
            "  |-> alsa_output.hw:playback_FL\n"
        )
        self.assertTrue(_contains_link(text, "mpv:output_FL", "fxroute_dsp_sink:playback_FL"))

    def test_missing_link_is_not_reported(self):
        text = (
            "\tfxroute_dsp_sink:playback_FL\n"
            "  |<- spotify:output_FL\n"
        )
        self.assertFalse(_contains_link(text, "mpv:output_FL", "fxroute_dsp_sink:playback_FL"))
        self.assertFalse(_contains_link(text, "mpv:output_FL", "fxroute_dsp_sink:playback_FR"))

    def test_arrow_text_form_is_still_recognized(self):
        self.assertTrue(_contains_link("mpv:output_FL -> fxroute_dsp_sink:playback_FL",
                                       "mpv:output_FL", "fxroute_dsp_sink:playback_FL"))


class PwLinkParsingTests(unittest.TestCase):
    """One parser feeds every consumer of the live `pw-link -l` graph."""

    TEXT = (
        "fxroute_dsp_sink:monitor_FL\n"
        "  |-> fxroute_dsp:input_1\n"
        "spotify:output_FL\n"
        "  |-> fxroute_dsp_sink:playback_FL\n"
        "  |-> alsa_output.hw:playback_AUX0\n"
        "fxroute_dsp:output_1\n"
        "  |-> alsa_output.hw:playback_AUX0\n"
        "alsa_output.hw:playback_AUX0\n"
        "  |<- fxroute_dsp:output_1\n"
    )

    def test_every_edge_is_yielded_with_its_port_header(self):
        self.assertEqual(list(iter_pw_links(self.TEXT)), [
            ("fxroute_dsp_sink:monitor_FL", "fxroute_dsp:input_1"),
            ("spotify:output_FL", "fxroute_dsp_sink:playback_FL"),
            ("spotify:output_FL", "alsa_output.hw:playback_AUX0"),
            ("fxroute_dsp:output_1", "alsa_output.hw:playback_AUX0"),
            ("fxroute_dsp:output_1", "alsa_output.hw:playback_AUX0"),
        ])

    def test_arrow_text_form_is_yielded(self):
        self.assertEqual(
            list(iter_pw_links("a:out -> b:in\n")),
            [("a:out", "b:in")],
        )

    def test_physical_output_links_keeps_only_dsp_to_playback_edges(self):
        self.assertEqual(
            physical_output_links(self.TEXT),
            {PipeWireLink("fxroute_dsp:output_1", "alsa_output.hw:playback_AUX0")},
        )

    def test_physical_output_links_matches_the_pre_extraction_parse(self):
        # Guard the shared parser extraction: the blank-line handling must stay
        # byte-for-byte the historic behavior for the diagnosis/readback paths.
        def historic(text):
            links = set()
            current = ""
            for raw in text.splitlines():
                line = raw.strip()
                if line.startswith("|-> "):
                    source, target = current, line[4:].strip()
                elif line.startswith("|<- "):
                    source, target = line[4:].strip(), current
                elif " -> " in line:
                    source, target = line.split(" -> ", 1)
                else:
                    current = line
                    continue
                if re.fullmatch(r"fxroute_dsp:output_\d+", source) and ":playback_" in target:
                    links.add(PipeWireLink(source, target))
            return links

        for text in (self.TEXT, "", "a:x\n\n  |-> b:y\n",
                     "fxroute_dsp:output_1\n  |-> hw:playback_AUX0"):
            self.assertEqual(physical_output_links(text), historic(text))


class PwLinkLiveShapeTests(unittest.TestCase):
    """Pin iter_pw_links against the real `pw-link -l` output of .104.

    Snapshot taken on .104 (PipeWire 1.6.8) with a healthy subwoofer-2.2
    graph: spotify playing through the helper to a Scarlett 16i16.  Every
    edge appears twice (once `|->` under the source port, once `|<-` under
    the target port); the parser must yield both so the set dedupes them.
    """

    HW = "alsa_output.usb-Focusrite_Scarlett_16i16_4th_Gen_S63ZKU25B036F6-00.multichannel-output"

    LIVE_104 = (
        "fxroute_dsp_sink:monitor_FL\n"
        "  |-> fxroute_dsp:input_1\n"
        "fxroute_dsp_sink:monitor_FR\n"
        "  |-> fxroute_dsp:input_2\n"
        "fxroute_dsp_sink:playback_FL\n"
        "  |<- spotify:output_FL\n"
        "fxroute_dsp_sink:playback_FR\n"
        "  |<- spotify:output_FR\n"
        "spotify:output_FL\n"
        "  |-> fxroute_dsp_sink:playback_FL\n"
        "spotify:output_FR\n"
        "  |-> fxroute_dsp_sink:playback_FR\n"
        f"{HW}:playback_AUX0\n"
        "  |<- fxroute_dsp:output_1\n"
        f"{HW}:playback_AUX1\n"
        "  |<- fxroute_dsp:output_2\n"
        f"{HW}:playback_AUX2\n"
        "  |<- fxroute_dsp:output_3\n"
        f"{HW}:playback_AUX3\n"
        "  |<- fxroute_dsp:output_4\n"
        "fxroute_dsp:output_1\n"
        f"  |-> {HW}:playback_AUX0\n"
        "fxroute_dsp:output_2\n"
        f"  |-> {HW}:playback_AUX1\n"
        "fxroute_dsp:output_3\n"
        f"  |-> {HW}:playback_AUX2\n"
        "fxroute_dsp:output_4\n"
        f"  |-> {HW}:playback_AUX3\n"
        "fxroute_dsp:input_1\n"
        "  |<- fxroute_dsp_sink:monitor_FL\n"
        "fxroute_dsp:input_2\n"
        "  |<- fxroute_dsp_sink:monitor_FR\n"
    )

    def test_live_output_yields_both_directions(self):
        edges = set(iter_pw_links(self.LIVE_104))
        self.assertIn(("spotify:output_FL", "fxroute_dsp_sink:playback_FL"), edges)
        self.assertIn(("spotify:output_FR", "fxroute_dsp_sink:playback_FR"), edges)
        self.assertIn(("fxroute_dsp:output_1", f"{self.HW}:playback_AUX0"), edges)
        self.assertIn(("fxroute_dsp_sink:monitor_FL", "fxroute_dsp:input_1"), edges)
        # 16 link lines, 8 distinct edges each seen twice.
        self.assertEqual(len(list(iter_pw_links(self.LIVE_104))), 16)
        self.assertEqual(len(edges), 8)

    def test_live_output_is_recognized(self):
        self.assertFalse(pw_link_output_unrecognized(self.LIVE_104, list(iter_pw_links(self.LIVE_104))))

    def test_live_output_physical_links(self):
        self.assertEqual(
            physical_output_links(self.LIVE_104),
            {PipeWireLink(f"fxroute_dsp:output_{index}", f"{self.HW}:playback_AUX{index - 1}")
             for index in (1, 2, 3, 4)},
        )

    def test_empty_output_is_recognized_as_no_links(self):
        self.assertEqual(list(iter_pw_links("")), [])
        self.assertFalse(pw_link_output_unrecognized("", []))

    def test_headers_only_are_recognized(self):
        text = "some:port\nanother:port\n"
        self.assertEqual(list(iter_pw_links(text)), [])
        self.assertFalse(pw_link_output_unrecognized(text, []))

    def test_arrow_form_is_recognized(self):
        edges = list(iter_pw_links("a:out -> b:in\n"))
        self.assertFalse(pw_link_output_unrecognized("a:out -> b:in\n", edges))

    def test_garbage_without_ports_is_unrecognized(self):
        for text in ("something broke\ntry again\n", "Error: host is down\n", "  |-> foo:bar\n"):
            with self.subTest(text=text):
                self.assertTrue(pw_link_output_unrecognized(text, list(iter_pw_links(text))))

    def test_healthy_live_graph_disconnects_nothing(self):
        commands = []

        async def run(command):
            commands.append(tuple(command))
            return CommandResult(0, self.LIVE_104 if tuple(command) == ("pw-link", "-l") else "")

        manager = DSPManager(home=Path(tempfile.mkdtemp()))
        manager.save_global_extras({"limiter": {"enabled": False}})
        runtime = DSPRuntime(manager, command_runner=run)
        runtime._config = DSPRuntimeConfig(
            "subwoofer-2.2", self.HW, 48000,
            tuple(f"playback_AUX{index}" for index in range(18)), (),
        )
        asyncio.run(runtime._remove_direct_source_links())
        self.assertEqual([command for command in commands if command[:2] == ("pw-link", "-d")], [])
        self.assertIn(("pw-link", "-l"), [command[:2] for command in commands])

    def test_unrecognized_output_falls_back_to_brute_force(self):
        from audio.output_ports import HARDWARE_CHANNEL_ORDER  # noqa: E402
        commands = []

        async def run(command):
            commands.append(tuple(command))
            return CommandResult(0, "something broke\ntry again\n"
                                 if tuple(command) == ("pw-link", "-l") else "")

        manager = DSPManager(home=Path(tempfile.mkdtemp()))
        manager.save_global_extras({"limiter": {"enabled": False}})
        runtime = DSPRuntime(manager, command_runner=run)
        runtime._config = DSPRuntimeConfig("stereo", "hw", 48000, ("playback_FL", "playback_FR"), ())
        asyncio.run(runtime._remove_direct_source_links())
        # Fail-safe: every modeled producer, hardware port and naming scheme.
        expected = {
            ("pw-link", "-d", f"{node}:{kind}_{channel}", f"hw:{port}")
            for node in ("mpv", "spotify", "alsa_playback.qbzd")
            for port in ("playback_FL", "playback_FR")
            for channel in HARDWARE_CHANNEL_ORDER
            for kind in ("output", "playback")
        }
        self.assertEqual({command for command in commands if command[:2] == ("pw-link", "-d")}, expected)


class ControlReplyTruncationTests(unittest.TestCase):
    def setUp(self):
        self.manager = DSPManager(home=Path(tempfile.mkdtemp()))
        self.manager.save_global_extras({"limiter": {"enabled": False}})

    def _runtime_with_datagram_pair(self, runtime, tmpdir):
        server_path = os.path.join(tmpdir, "server.sock")
        client_path = os.path.join(tmpdir, "client.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(server_path)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        client.setblocking(False)
        client.bind(client_path)
        runtime._control_socket = client
        runtime._control_path = Path(server_path)
        return server, client, client_path

    def test_oversized_control_reply_is_rejected(self):
        async def exercise():
            runtime = DSPRuntime(self.manager)
            tmpdir = tempfile.mkdtemp()
            server, client, client_path = self._runtime_with_datagram_pair(runtime, tmpdir)
            try:
                server.sendto(b"x" * (CONTROL_REPLY_MAX_BYTES + 1), client_path)
                with self.assertRaises(RuntimeError) as ctx:
                    await runtime._control_unlocked("peaks get", reply=True)
                self.assertIn("truncated", str(ctx.exception))
            finally:
                client.close()
                server.close()

        asyncio.run(exercise())

    def test_bounded_control_reply_is_parsed(self):
        async def exercise():
            runtime = DSPRuntime(self.manager)
            tmpdir = tempfile.mkdtemp()
            server, client, client_path = self._runtime_with_datagram_pair(runtime, tmpdir)
            try:
                server.sendto(b"ok\n", client_path)
                result = await runtime._control_unlocked("peaks get", reply=True)
                self.assertEqual(result, "ok\n")
            finally:
                client.close()
                server.close()

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
