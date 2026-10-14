#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Device routing identity and native physical-link behavior.

Topology lives in the v2 output state; the processing plan carries the
physical routes the native runtime links.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio import output_routing as routing
from dsp.runtime import CommandResult, DSPRuntime, DSPRuntimeConfig


class RoutingTests(unittest.TestCase):
    def test_usb_device_key_collapses_profiles(self):
        key = "alsa_output.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-output"
        pro = key.rsplit(".", 1)[0] + ".pro-output-0"
        self.assertEqual(routing.device_key(key), routing.device_key(pro))
        self.assertEqual(routing.device_key(key), "alsa_output.usb-Focusrite_Scarlett_16i16_4th_Gen-00")


class NativeRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_swap_fanout_and_off_reconcile_actual_hardware_edges(self):
        ports = [f"playback_AUX{i}" for i in range(6)]
        layout = [{"name": role, "routes": []} for role in ("main_l", "main_r", "sub1", "sub2")]
        # Channel 0 plays main_r, 1 and 3 fan out main_l, 2 is off, 4/5 swap the subs.
        plan = {"mode": "stereo-sub", "physical_routes": [
            {"output": 1, "channel": 0}, {"output": 0, "channel": 1}, {"output": 0, "channel": 3},
            {"output": 3, "channel": 4}, {"output": 2, "channel": 5}]}
        config = DSPRuntimeConfig.from_plan(plan, layout=layout, output_key="alsa_output.test",
                                            sample_rate_hz=48000, hardware_ports=ports)
        edges = {("fxroute_dsp:output_1", "alsa_output.test:playback_AUX0")}

        async def run(args):
            if args == ("pw-link", "-l"):
                text = "\n".join(f"{src}\n  |-> {dst}" for src, dst in edges)
                return CommandResult(0, text, "")
            if args[:2] == ("pw-link", "-d"):
                edges.discard(tuple(args[2:]))
            else:
                self.assertEqual(args[0], "pw-link")
                edges.add(tuple(args[1:]))
            return CommandResult(0, "", "")

        runtime = DSPRuntime(None, command_runner=run)
        await runtime._reconcile_output_links(config)
        self.assertEqual(edges, {
            ("fxroute_dsp:output_2", "alsa_output.test:playback_AUX0"),
            ("fxroute_dsp:output_1", "alsa_output.test:playback_AUX1"),
            ("fxroute_dsp:output_1", "alsa_output.test:playback_AUX3"),
            ("fxroute_dsp:output_4", "alsa_output.test:playback_AUX4"),
            ("fxroute_dsp:output_3", "alsa_output.test:playback_AUX5"),
        })


if __name__ == "__main__":
    unittest.main()
