#!/usr/bin/env python3
import asyncio
import os
import signal
import socket
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp_manager import DSPManager
from dsp_runtime import (CommandResult, DSPRuntime, DSPRuntimeConfig, PipeWireLink,
                         CONTROL_REPLY_MAX_BYTES, DSP_INGRESS_MONITOR_NODE,
                         RUNTIME_COMMAND_TIMEOUT_RETURNCODE,
                         RUNTIME_COMMAND_TIMEOUT_SECONDS, _contains_link)

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
        self.assertEqual(config.hardware_ports, ("playback_FL", "playback_FR", "playback_RL", "playback_RR"))
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

    def test_engine_text_emits_every_enabled_stage_in_exact_order(self):
        ir = self.manager.irs_dir / "room.irs"
        ir.write_bytes(b"ir")
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
        self.manager.preset_store.write("Ordered", self.manager._native_preset(chain))
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
        self.manager.preset_store.write("Dual", self.manager._native_preset(chain))
        text = self.manager.compile_engine_text(
            [{"name": "FL", "source": 0}, {"name": "FR", "source": 1}], preset_name="Dual")
        for expected in ("control g_in 0.794328235", "control g_out 1.41253754",
                          "control mode 3", "control clink 0", "control ftl_0 1",
                          "control fml_0 0", "control sl_0 0", "control fl_0 100",
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
        self.manager.preset_store.write("Types", self.manager._native_preset(chain))
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
        self.manager.preset_store.write("Legacy PEQ", self.manager._native_preset(chain))
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
        self.manager.preset_store.write("Long PEQ Delay", self.manager._native_preset(chain))
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
        self.manager.preset_store.write("LV2", self.manager._native_preset(chain))
        text = self.manager.compile_engine_text(
            [{"name": "FL", "source": 0}, {"name": "FR", "source": 1}], preset_name="LV2")
        self.assertIn("lv2 http://calf.sourceforge.net/plugins/BassEnhancer", text)
        for expected in ("control amount 1.77827941", "control drive 8.5",
                         "control freq 100", "control blend 2", "control listen 0",
                         "control floor_active 0", "control floor 20"):
            self.assertIn(expected, text)
        self.assertIn("lv2 http://lsp-plug.in/plugins/lv2/loud_comp_stereo", text)
        for expected in ("control input 1", "control mode 0", "control std 4",
                         "control fft 5", "control approx 2", "control volume -7.5",
                         "control hclip 0", "control hcrange 6"):
            self.assertIn(expected, text)
        self.assertIn("param output_gain_db 7.5", text)
        self.assertIn("lv2 http://lsp-plug.in/plugins/lv2/sc_limiter_stereo", text)
        for expected in ("control g_in 0.707945784", "control g_out 1.25892541",
                          "control th 0.794328235", "control at 4", "control rt 20",
                          "control lk 6", "control slink 80", "control alr 0",
                          "control boost 1", "control extsc 0", "control mode 0",
                          "control ovs 0", "control dith 0", "control scp 1",
                          "control in2lk 0", "control sc2lk 0"):
            self.assertIn(expected, text)
        self.assertIn("lv2 urn:zamaudio:ZaMaximX2", text)
        self.assertIn("control thresh -0.5", text)
        self.assertIn("control rel 30", text)

    def test_output_alignment_follows_chain(self):
        chain = [{"id": "delay#0", "type": "delay", "enabled": True,
                  "params": {"leftMs": 1, "rightMs": 2}}]
        self.manager.preset_store.write("Placement", self.manager._native_preset(chain))
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
                runtime._config = DSPRuntimeConfig.from_overview(self.overview("stereo"))
                runtime._run = lambda _command: complete()
                runtime._stop_orphan_helpers = lambda: complete()
                runtime._reconcile_output_links = lambda _config: complete()
                runtime._control = lambda command, **kwargs: record_async(events, ("control", command.split()[0:2]))
                await runtime.sync(self.overview("stereo"))

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

    def test_incompatible_config_still_uses_rebuild_path(self):
        runtime = DSPRuntime(self.manager)
        runtime._config = DSPRuntimeConfig.from_overview(self.overview("stereo"))
        changed = self.overview("stereo")
        changed["output_mode"]["effective_output_key"] = "other-hw"
        self.assertFalse(runtime._can_hot_update(DSPRuntimeConfig.from_overview(changed)))

    def test_swap_reconcile_failure_keeps_config_for_fallback_rebuild(self):
        events = []

        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / "fxroute-dsp"
                binary.touch()
                runtime = DSPRuntime(self.manager, binary=binary)
                runtime._process = FakeProcess()
                runtime._control_socket = unittest.mock.Mock()
                runtime._config = DSPRuntimeConfig.from_overview(self.overview("stereo"))
                runtime._links = []

                async def run(command):
                    events.append(("run", tuple(command)))
                    return CommandResult(0, "")

                async def launch(command):
                    events.append(("launch", tuple(command), Path(command[1]).exists()))
                    return FakeProcess()

                async def control(command, **_kwargs):
                    events.append(("control", command))
                    return "0\n" if command == "effects bypass get" else "ok\n"

                runtime._run = run
                runtime._launch = launch
                runtime._control = control
                runtime._stop_orphan_helpers = lambda: complete()
                runtime._wait_for_ports = lambda _config: complete()
                runtime._remove_direct_source_links = lambda: complete()
                runtime._reconcile_output_links = lambda _config: fail_reconcile()
                await runtime.sync(self.overview("stereo"))
                await runtime.stop()

        async def complete():
            return None

        async def fail_reconcile():
            raise RuntimeError("link failed")

        asyncio.run(exercise())
        launch_events = [event for event in events if event[0] == "launch"]
        self.assertEqual(len(launch_events), 1)
        self.assertTrue(launch_events[0][2])

    def test_runtime_snapshot_reports_actual_layout_and_effect_bypass(self):
        runtime = DSPRuntime(self.manager)
        runtime._config = DSPRuntimeConfig.from_overview(self.overview("subwoofer-2.1"))
        runtime._effect_bypass = True
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot["config"]["sample_rate"], 48000)
        self.assertEqual(snapshot["config"]["output_mode"], "subwoofer-2.1")
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

    def test_guarded_rebuild_starts_candidate_at_guard_and_ramps(self):
        events = []

        async def exercise():
            runtime = DSPRuntime(self.manager)
            runtime.set_output_gain_db = lambda value: record_async(events, ("gain", value))
            runtime._sync = lambda overview, **kwargs: record_async(
                events, ("sync", kwargs["initial_output_gain_db"]))
            runtime.ramp_output_gain_db = lambda start, target, **kwargs: record_async(
                events, ("ramp", start, target))
            await runtime.guarded_rebuild(
                self.overview("stereo"), guard_db=-18,
                apply_candidate=lambda: events.append(("apply", "new")),
                apply_previous=lambda: events.append(("apply", "old")),
                settle_seconds=0)

        async def record_async(target, event):
            target.append(event)

        asyncio.run(exercise())
        self.assertEqual(events, [
            ("gain", -18), ("sync", -18), ("ramp", -18, 0.0), ("apply", "new"),
        ])

    def test_guarded_rebuild_rolls_back_previous_state_on_failure(self):
        events = []

        async def exercise():
            runtime = DSPRuntime(self.manager)
            runtime.set_output_gain_db = lambda value: record(("gain", value))
            sync_count = 0
            async def sync(_overview, **kwargs):
                nonlocal sync_count
                sync_count += 1
                events.append(("sync", sync_count, kwargs["initial_output_gain_db"]))
                if sync_count == 1:
                    raise RuntimeError("candidate failed")
            runtime._sync = sync
            runtime.ramp_output_gain_db = lambda start, target, **kwargs: record(("ramp", start, target))
            with self.assertRaisesRegex(RuntimeError, "candidate failed"):
                await runtime.guarded_rebuild(
                    self.overview("stereo"), guard_db=-24,
                    apply_candidate=lambda: events.append(("apply", "new")),
                    apply_previous=lambda: events.append(("apply", "old")),
                    settle_seconds=0)

        async def record(event):
            events.append(event)

        asyncio.run(exercise())
        self.assertEqual(events, [
            ("gain", -24), ("sync", 1, -24),
            ("apply", "old"), ("sync", 2, -24), ("ramp", -24, 0.0),
        ])

    def test_guarded_rebuild_preserves_original_error_when_rollback_fails(self):
        async def exercise():
            runtime = DSPRuntime(self.manager)
            runtime.set_output_gain_db = lambda _value: complete()
            async def sync(_overview, **_kwargs):
                raise RuntimeError("candidate failed")
            runtime._sync = sync
            with self.assertRaisesRegex(RuntimeError, "candidate failed"):
                await runtime.guarded_rebuild(
                    self.overview("stereo"), guard_db=-18,
                    apply_candidate=lambda: None,
                    apply_previous=lambda: (_ for _ in ()).throw(RuntimeError("rollback failed")),
                    settle_seconds=0)

        async def complete():
            return None

        asyncio.run(exercise())

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

    def test_reclean_reconciles_stable_native_sub_output_links(self):
        commands = []

        async def run(command):
            commands.append(tuple(command))
            return CommandResult(0, "")

        runtime = DSPRuntime(self.manager, command_runner=run)
        runtime._config = DSPRuntimeConfig.from_overview(self.overview("subwoofer-2.1"))
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
            runtime._config = DSPRuntimeConfig.from_overview(self.overview("stereo"))
            await runtime._lock.acquire()
            task = asyncio.create_task(runtime._reclean_guarded())
            await asyncio.sleep(0)
            self.assertEqual(events, [])
            runtime._lock.release()
            await task

        asyncio.run(exercise())

    def test_compile_failure_preserves_running_runtime(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / "fxroute-dsp"
                binary.touch()
                old_config = Path(directory) / "old.conf"
                old_config.write_text("old")
                old_process = FakeProcess()
                runtime = DSPRuntime(self.manager, binary=binary)
                runtime._process = old_process
                runtime._config = DSPRuntimeConfig.from_overview(self.overview("stereo"))
                runtime._config_path = old_config
                runtime._links = [PipeWireLink("old-source", "old-target")]
                original_compile = self.manager.compile_engine_text
                self.manager.compile_engine_text = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("compile failed"))
                try:
                    with self.assertRaisesRegex(RuntimeError, "compile failed"):
                        await runtime.sync(self.overview("stereo"))
                finally:
                    self.manager.compile_engine_text = original_compile
                self.assertIs(runtime._process, old_process)
                self.assertFalse(old_process.terminated)
                self.assertEqual(runtime._config_path, old_config)
                self.assertTrue(old_config.exists())
                self.assertEqual(runtime._links, [PipeWireLink("old-source", "old-target")])

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
                runtime._config = DSPRuntimeConfig.from_overview(self.overview("stereo"))
                runtime._config_path = old_config
                runtime._links = [PipeWireLink("old-source", "old-target")]
                with self.assertRaisesRegex(RuntimeError, "plugin failed"):
                    await runtime.sync(self.overview("stereo"))
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
                runtime._config = DSPRuntimeConfig.from_overview(self.overview("stereo"))
                runtime._links = [PipeWireLink("old-source", "old-target")]
                with self.assertRaisesRegex(RuntimeError, "launch failed"):
                    await runtime.sync(self.overview("stereo"))
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
        import dsp_runtime as runtime_module

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
