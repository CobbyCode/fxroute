#!/usr/bin/env python3
"""Smoke tests for the 2.1 native helper runtime controller."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subwoofer_runtime import CommandResult, NATIVE_HELPER_PENDING_MESSAGE, Subwoofer21Runtime, SubwooferRuntimeConfig


def make_config(**overrides):
    values = {
        "output_mode": "subwoofer-2.1",
        "output_key": "mock_multichannel_output",
        "output_label": "Mock 4-channel output",
        "output_channels": 4,
        "sample_rate": 48_000,
        "crossover_frequency_hz": 120,
        "main_highpass_enabled": False,
        "sub_level_db": 0.0,
        "sub_alignment_ms": 0.0,
        "sub_polarity": "normal",
    }
    values.update(overrides)
    return SubwooferRuntimeConfig(**values)


class FakeProcess:
    def __init__(self):
        self.pid = 12345
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def mock_graph_links(output_key="mock_output"):
    return "\n".join(
        [
            "ee_soe_output_level:output_FL",
            "  |-> fxroute_21_stage1:input_L",
            "ee_soe_output_level:output_FR",
            "  |-> fxroute_21_stage1:input_R",
            "fxroute_21_stage1:output_1",
            f"  |-> {output_key}:playback_FL",
            "fxroute_21_stage1:output_2",
            f"  |-> {output_key}:playback_FR",
            "fxroute_21_stage1:output_3",
            f"  |-> {output_key}:playback_RL",
            "fxroute_21_stage1:output_4",
            f"  |-> {output_key}:playback_RR",
        ]
    )


def arg_value(args, flag):
    return args[args.index(flag) + 1]


class SubwooferRuntimeStatusTest(unittest.TestCase):
    def test_config_follows_effective_output_rate(self):
        config = SubwooferRuntimeConfig.from_overview({
            "output_mode": {
                "mode": "subwoofer-2.1",
                "effective_output_key": "mock_output",
                "effective_output_channels": 4,
                "effective_output_rate": 96_000,
                "subwoofer": {
                    "crossover_frequency_hz": 500,
                    "sub_level_db": 99,
                    "sub_alignment_ms": -5,
                    "sub_polarity": "180",
                },
            },
            "selected_output": {"label": "Mock Output", "active_rate": 44_100},
        })

        self.assertEqual(config.output_mode, "subwoofer-2.1")
        self.assertEqual(config.output_key, "mock_output")
        self.assertEqual(config.output_channels, 4)
        self.assertEqual(config.sample_rate, 96_000)
        self.assertEqual(config.crossover_frequency_hz, 200)
        self.assertEqual(config.sub_level_db, 12.0)
        self.assertEqual(config.sub_alignment_ms, -5.0)
        self.assertEqual(config.derived_main_delay_ms, 5.0)
        self.assertEqual(config.derived_sub_delay_ms, 0.0)
        self.assertEqual(config.sub_polarity, "invert")

    def test_subwoofer_mode_reports_missing_helper_binary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_helper = Path(temp_dir) / "missing_fxroute_21_helper"
            runtime = Subwoofer21Runtime(helper_binary=missing_helper)
            asyncio.run(runtime.sync(make_config()))

            snapshot = runtime.snapshot()

            self.assertFalse(snapshot["active"])
            self.assertEqual(snapshot["stage"], "stage4_sub_controls")
            self.assertEqual(snapshot["engine"], "pipewire_native_helper")
            self.assertTrue(snapshot["implemented"])
            self.assertIn(NATIVE_HELPER_PENDING_MESSAGE, snapshot["last_error"])
            self.assertIn(NATIVE_HELPER_PENDING_MESSAGE, snapshot["inactive_reason"])

    def test_stereo_mode_has_no_runtime_error(self):
        runtime = Subwoofer21Runtime()
        asyncio.run(runtime.sync(make_config(output_mode="stereo", output_channels=2)))

        snapshot = runtime.snapshot()

        self.assertFalse(snapshot["active"])
        self.assertIsNone(snapshot["last_error"])
        self.assertEqual(snapshot["stage"], "stage4_sub_controls")

    def test_stage3_helper_starts_and_links_graph(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            helper = Path(temp_dir) / "fxroute_21_passthrough"
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            commands = []
            processes = []

            async def fake_runner(args):
                commands.append(tuple(args))
                if tuple(args) == ("pw-link", "-io"):
                    return CommandResult(
                        0,
                        "\n".join(
                            [
                                "fxroute_21_stage1:input_L",
                                "fxroute_21_stage1:input_R",
                                "fxroute_21_stage1:output_1",
                                "fxroute_21_stage1:output_2",
                                "fxroute_21_stage1:output_3",
                                "fxroute_21_stage1:output_4",
                            ]
                        ),
                        "",
                    )
                if tuple(args) == ("pw-link", "-l"):
                    return CommandResult(0, mock_graph_links("mock_output"), "")
                return CommandResult(0, "", "")

            async def fake_launcher(args):
                commands.append(tuple(args))
                process = FakeProcess()
                processes.append(process)
                return process

            async def fake_sleep(_seconds):
                return None

            runtime = Subwoofer21Runtime(
                helper_binary=helper,
                command_runner=fake_runner,
                process_launcher=fake_launcher,
                sleeper=fake_sleep,
            )
            asyncio.run(runtime.sync(make_config(output_key="mock_output", sample_rate=44_100)))

            snapshot = runtime.snapshot()

            self.assertTrue(snapshot["active"])
            self.assertIsNone(snapshot["last_error"])
            self.assertEqual(snapshot["helper_pid"], 12345)
            self.assertEqual(snapshot["removed_direct_front_links"], 4)
            helper_command = next(command for command in commands if command and command[0] == str(helper))
            self.assertEqual(arg_value(helper_command, "--rate"), "44100")
            self.assertEqual(arg_value(helper_command, "--lowpass-hz"), "120")
            self.assertEqual(arg_value(helper_command, "--highpass-hz"), "0")
            self.assertEqual(arg_value(helper_command, "--bass-routing"), "mono")
            self.assertEqual(arg_value(helper_command, "--sub-delay-ms"), "0.0")
            self.assertEqual(arg_value(helper_command, "--sub2-delay-ms"), "0.0")
            self.assertIn(("pw-link", "ee_soe_output_level:output_FL", "fxroute_21_stage1:input_L"), commands)
            self.assertIn(("pw-link", "fxroute_21_stage1:output_3", "mock_output:playback_RL"), commands)
            self.assertIn(("pw-link", "fxroute_21_stage1:output_4", "mock_output:playback_RR"), commands)
            self.assertIn(("pw-link", "-d", "ee_soe_output_level:output_FL", "mock_output:playback_FL"), commands)

            asyncio.run(runtime.sync(make_config(output_mode="stereo", output_channels=2)))

            self.assertTrue(processes[0].terminated)
            self.assertFalse(runtime.snapshot()["active"])
            self.assertIn(("pw-link", "-d", "fxroute_21_stage1:output_3", "mock_output:playback_RL"), commands)

    def test_main_highpass_enabled_passes_crossover_to_helper(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            helper = Path(temp_dir) / "fxroute_21_passthrough"
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            commands = []

            async def fake_runner(args):
                commands.append(tuple(args))
                if tuple(args) == ("pw-link", "-io"):
                    return CommandResult(
                        0,
                        "\n".join(
                            [
                                "fxroute_21_stage1:input_L",
                                "fxroute_21_stage1:input_R",
                                "fxroute_21_stage1:output_1",
                                "fxroute_21_stage1:output_2",
                                "fxroute_21_stage1:output_3",
                                "fxroute_21_stage1:output_4",
                            ]
                        ),
                        "",
                    )
                if tuple(args) == ("pw-link", "-l"):
                    return CommandResult(0, mock_graph_links("mock_multichannel_output"), "")
                return CommandResult(0, "", "")

            async def fake_launcher(args):
                commands.append(tuple(args))
                return FakeProcess()

            runtime = Subwoofer21Runtime(
                helper_binary=helper,
                command_runner=fake_runner,
                process_launcher=fake_launcher,
                sleeper=lambda _seconds: asyncio.sleep(0),
            )
            asyncio.run(runtime.sync(make_config(main_highpass_enabled=True, crossover_frequency_hz=87)))

            helper_command = next(command for command in commands if command and command[0] == str(helper))
            self.assertEqual(arg_value(helper_command, "--lowpass-hz"), "87")
            self.assertEqual(arg_value(helper_command, "--highpass-hz"), "87")
            self.assertEqual(arg_value(helper_command, "--bass-routing"), "mono")

    def test_existing_pipewire_link_is_treated_as_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            helper = Path(temp_dir) / "fxroute_21_passthrough"
            helper.write_text("#!/bin/sh\n", encoding="utf-8")

            async def fake_runner(args):
                if tuple(args) == ("pw-link", "-io"):
                    return CommandResult(
                        0,
                        "\n".join(
                            [
                                "fxroute_21_stage1:input_L",
                                "fxroute_21_stage1:input_R",
                                "fxroute_21_stage1:output_1",
                                "fxroute_21_stage1:output_2",
                                "fxroute_21_stage1:output_3",
                                "fxroute_21_stage1:output_4",
                            ]
                        ),
                        "",
                    )
                if tuple(args) == ("pw-link", "-l"):
                    return CommandResult(0, mock_graph_links("mock_multichannel_output"), "")
                if len(args) == 3 and args[0] == "pw-link" and args[1] == "ee_soe_output_level:output_FL":
                    return CommandResult(1, "", "failed to link ports: File exists")
                return CommandResult(0, "", "")

            runtime = Subwoofer21Runtime(
                helper_binary=helper,
                command_runner=fake_runner,
                process_launcher=lambda _args: asyncio.sleep(0, result=FakeProcess()),
                sleeper=lambda _seconds: asyncio.sleep(0),
            )
            asyncio.run(runtime.sync(make_config()))

            self.assertTrue(runtime.snapshot()["active"])
            self.assertIsNone(runtime.snapshot()["last_error"])

    def test_concurrent_reconfigs_are_coalesced_to_latest_config(self):
        async def run_case():
            with tempfile.TemporaryDirectory() as temp_dir:
                helper = Path(temp_dir) / "fxroute_21_passthrough"
                helper.write_text("#!/bin/sh\n", encoding="utf-8")
                first_wait_started = asyncio.Event()
                allow_first_wait = asyncio.Event()
                first_wait = True
                launched = []

                async def fake_runner(args):
                    nonlocal first_wait
                    if tuple(args) == ("pw-link", "-io"):
                        if first_wait:
                            first_wait = False
                            first_wait_started.set()
                            await allow_first_wait.wait()
                        return CommandResult(
                            0,
                            "\n".join(
                                [
                                    "fxroute_21_stage1:input_L",
                                    "fxroute_21_stage1:input_R",
                                    "fxroute_21_stage1:output_1",
                                    "fxroute_21_stage1:output_2",
                                    "fxroute_21_stage1:output_3",
                                    "fxroute_21_stage1:output_4",
                                ]
                            ),
                            "",
                        )
                    if tuple(args) == ("pw-link", "-l"):
                        return CommandResult(0, mock_graph_links("mock_multichannel_output"), "")
                    return CommandResult(0, "", "")

                async def fake_launcher(args):
                    launched.append(tuple(args))
                    process = FakeProcess()
                    process.pid = 12345 + len(launched)
                    return process

                runtime = Subwoofer21Runtime(
                    helper_binary=helper,
                    command_runner=fake_runner,
                    process_launcher=fake_launcher,
                    sleeper=lambda _seconds: asyncio.sleep(0),
                )
                first = asyncio.create_task(runtime.sync(make_config(crossover_frequency_hz=52)))
                await first_wait_started.wait()
                await runtime.sync(make_config(crossover_frequency_hz=93))
                allow_first_wait.set()
                await first
                return launched, runtime.snapshot()

        launched, snapshot = asyncio.run(run_case())

        self.assertEqual(len(launched), 2)
        self.assertIn("--lowpass-hz", launched[0])
        self.assertEqual(launched[0][launched[0].index("--lowpass-hz") + 1], "52")
        self.assertEqual(launched[1][launched[1].index("--lowpass-hz") + 1], "93")
        self.assertTrue(snapshot["active"])
        self.assertEqual(snapshot["config"]["crossover_frequency_hz"], 93)

    def test_signed_alignment_derives_exclusive_branch_delays(self):
        positive = make_config(sub_alignment_ms=5.0)
        negative = make_config(sub_alignment_ms=-5.0)
        zero = make_config(sub_alignment_ms=0.0)

        self.assertEqual(positive.derived_main_delay_ms, 0.0)
        self.assertEqual(positive.derived_sub_delay_ms, 5.0)
        self.assertEqual(negative.derived_main_delay_ms, 5.0)
        self.assertEqual(negative.derived_sub_delay_ms, 0.0)
        self.assertEqual(zero.derived_main_delay_ms, 0.0)
        self.assertEqual(zero.derived_sub_delay_ms, 0.0)

    def test_22_stereo_uses_stereo_bass_routing_and_combined_delays(self):
        config = make_config(
            output_mode="subwoofer-2.2-stereo",
            sub_alignment_ms=-2.0,
            sub2_alignment_ms=5.0,
        )

        self.assertEqual(config.bass_routing, "stereo")
        self.assertEqual(config.derived_main_delay_ms, 2.0)
        self.assertEqual(config.derived_sub1_delay_ms, 0.0)
        self.assertEqual(config.derived_sub2_delay_ms, 7.0)

    def test_22_stereo_helper_starts_with_stereo_bass_routing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            helper = Path(temp_dir) / "fxroute_21_passthrough"
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            commands = []

            async def fake_runner(args):
                commands.append(tuple(args))
                if tuple(args) == ("pw-link", "-io"):
                    return CommandResult(
                        0,
                        "\n".join(
                            [
                                "fxroute_21_stage1:input_L",
                                "fxroute_21_stage1:input_R",
                                "fxroute_21_stage1:output_1",
                                "fxroute_21_stage1:output_2",
                                "fxroute_21_stage1:output_3",
                                "fxroute_21_stage1:output_4",
                            ]
                        ),
                        "",
                    )
                if tuple(args) == ("pw-link", "-l"):
                    return CommandResult(0, mock_graph_links("mock_multichannel_output"), "")
                return CommandResult(0, "", "")

            async def fake_launcher(args):
                commands.append(tuple(args))
                return FakeProcess()

            runtime = Subwoofer21Runtime(
                helper_binary=helper,
                command_runner=fake_runner,
                process_launcher=fake_launcher,
                sleeper=lambda _seconds: asyncio.sleep(0),
            )
            asyncio.run(runtime.sync(make_config(output_mode="subwoofer-2.2-stereo")))

            helper_command = next(command for command in commands if command and command[0] == str(helper))
            self.assertEqual(arg_value(helper_command, "--bass-routing"), "stereo")


if __name__ == "__main__":
    unittest.main()
