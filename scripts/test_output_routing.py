#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Signal-assignment defaults and native physical-link behavior.

Backend-v2 migration: numeric routing persistence (save/restore) is
deleted; the overview carries fixed default assignments and topology
lives in the v2 output state. What remains is the read-only default
payload, assignment validation, and the native link reconciliation.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio import output_routing as routing
from dsp.runtime import CommandResult, DSPRuntime, DSPRuntimeConfig


class RoutingTests(unittest.TestCase):
    def test_stereo_device_has_no_matrix(self):
        self.assertFalse(routing.routing_payload("stereo", 2)["available"])

    def test_multichannel_defaults_are_fixed(self):
        # No persistence remains: every device reads the same defaults.
        self.assertEqual(routing.routing_payload("A", 6)["assignments"], [1, 2, 3, 4, 0, 0])
        self.assertEqual(routing.routing_payload("B", 6)["assignments"], [1, 2, 3, 4, 0, 0])
        self.assertFalse(routing.routing_payload("A", 6)["customized"])

    def test_usb_device_key_collapses_profiles(self):
        key = "alsa_output.usb-Focusrite_Scarlett_16i16_4th_Gen-00.multichannel-output"
        pro = key.rsplit(".", 1)[0] + ".pro-output-0"
        self.assertEqual(routing.device_key(key), routing.device_key(pro))
        self.assertEqual(routing.routing_payload(pro, 4)["device_key"],
                         routing.routing_payload(key, 6)["device_key"])

    def test_invalid_assignments_are_rejected(self):
        for values in ([1, 2], [1, 2, 3, 5], [True, 2, 3, 4], [1, 2, 3, "4"]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                routing.validate_assignments(values, 4)
        self.assertEqual(routing.validate_assignments([2, 1, 0, 1], 4), [2, 1, 0, 1])
        with self.assertRaises(ValueError):
            routing.validate_assignments([1, 2], 2)


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
