#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Persistent signal assignment and native physical-link behavior."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio import output_routing as routing
from dsp.runtime import CommandResult, DSPRuntime, DSPRuntimeConfig


class RoutingTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": root.name})
        env.start()
        self.addCleanup(env.stop)

    def test_stereo_device_has_no_matrix(self):
        self.assertFalse(routing.routing_payload("stereo", 2)["available"])

    def test_multichannel_defaults_and_device_isolation(self):
        self.assertEqual(routing.routing_payload("A", 6)["assignments"], [1, 2, 3, 4, 0, 0])
        routing.save_assignments("A", [2, 1, 0, 1, 4, 3], 6)
        self.assertEqual(routing.routing_payload("A", 6)["assignments"], [2, 1, 0, 1, 4, 3])
        self.assertEqual(routing.routing_payload("B", 6)["assignments"], [1, 2, 3, 4, 0, 0])

    def test_smaller_tier_keeps_dormant_assignments(self):
        key = "alsa_output.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-output"
        routing.save_assignments(key, [1, 2, 3, 4, 1, 2], 6)
        pro = key.rsplit(".", 1)[0] + ".pro-output-0"
        self.assertEqual(routing.routing_payload(pro, 4)["inactive_assignments"], [5, 6])
        routing.save_assignments(pro, [2, 1, 3, 4], 4)
        self.assertEqual(routing.routing_payload(key, 6)["assignments"], [2, 1, 3, 4, 1, 2])

    def test_invalid_assignments_do_not_change_saved_routes(self):
        routing.save_assignments("A", [1, 2, 3, 4], 4)
        for values in ([1, 2], [1, 2, 3, 5], [True, 2, 3, 4], [1, 2, 3, "4"]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                routing.save_assignments("A", values, 4)
        self.assertEqual(routing.routing_payload("A", 4)["assignments"], [1, 2, 3, 4])


class NativeRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_swap_fanout_and_off_reconcile_actual_hardware_edges(self):
        ports = [f"playback_AUX{i}" for i in range(6)]
        mode = {"mode": "subwoofer-2.2", "effective_output_key": "alsa_output.test",
                "effective_output_channels": 6, "effective_output_rate": 48000,
                "output_routing": {"assignments": [2, 1, 0, 1, 4, 3]}}
        config = DSPRuntimeConfig.from_overview({"output_mode": mode}, hardware_ports=ports)
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
        self.assertEqual([row["name"] for row in config.layout], ["FL", "FR", "SUB1", "SUB2"])


if __name__ == "__main__":
    unittest.main()
