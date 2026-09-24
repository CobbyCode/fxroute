#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Routing-derived topology, fan-out, and hardware capacity contracts."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_topology import derive_topology


class OutputTopologyTests(unittest.TestCase):
    def test_sub_logic_depends_on_unique_roles_not_port_count(self):
        cases = [
            ([], "none"), (["sub_r"], "mono"), (["sub1", "sub1"], "mono"),
            (["sub_l", "sub_r"], "stereo"), (["sub_r", "sub_l"], "stereo"),
            (["sub1", "sub2"], "dual-mono"), (["sub1", "sub_r"], "dual-mono"),
            (["sub_l", "sub2"], "dual-mono"),
        ]
        for subs, expected in cases:
            with self.subTest(subs=subs):
                topology = derive_topology("stereo-sub", ["main_l", "main_r", *subs])
                self.assertEqual(topology.sub_mode, expected)
                self.assertEqual(topology.issues, ())

    def test_three_port_system_and_fanout_are_activatable(self):
        topology = derive_topology("stereo-sub", ["main_l", "main_r", "sub1", "sub1"], channels=3)
        topology.require_activatable()
        self.assertEqual(topology.roles, ("main_l", "main_r", "sub1"))

    def test_way_count_uses_roles_instead_of_assignment_order(self):
        for ways in (("low", "high"), ("low", "mid", "high"), ("low", "low_mid", "mid", "high")):
            assignments = [f"{side}_{way}" for side in ("left", "right") for way in ways]
            topology = derive_topology("stereo-sub", list(reversed(assignments)) + ["sub1", "off"],
                                       crossover_enabled=True)
            topology.require_activatable()
            self.assertEqual(topology.way_count, len(ways))
            self.assertEqual(topology.left_ways, tuple(f"left_{way}" for way in ways))
            self.assertEqual(topology.right_ways, tuple(f"right_{way}" for way in ways))
            self.assertEqual(topology.sub_mode, "mono")
            self.assertEqual(topology.bank_ids, ("global", *ways, "sub1"))

    def test_stereo_without_subs_uses_only_stereo_roles(self):
        topology = derive_topology("stereo", ["main_l", "main_r"])
        topology.require_activatable()
        self.assertEqual(topology.sub_mode, "none")
        topology = derive_topology("stereo", ["left_low", "left_high", "right_low", "right_high"],
                                   crossover_enabled=True)
        topology.require_activatable()
        self.assertEqual(topology.way_count, 2)

    def test_incomplete_drafts_are_retained_but_not_activatable(self):
        for mode, assignments, crossover in (
            ("stereo-sub", ["main_l", "off"], False),
            ("stereo-sub", ["main_l", "main_r", "sub1", "sub2", "sub_r"], False),
            ("stereo", [], False),
            ("stereo", ["left_low", "left_high", "right_low"], True),
            ("stereo", ["left_low", "left_high", "right_low", "right_mid", "right_high"], True),
            ("stereo-sub", ["left_low", "left_low_mid", "left_high", "right_low", "right_low_mid", "right_high"], True),
        ):
            with self.subTest(assignments=assignments):
                topology = derive_topology(mode, assignments, crossover_enabled=crossover)
                self.assertTrue(topology.issues)
                with self.assertRaises(ValueError):
                    topology.require_activatable()

    def test_dormant_ports_do_not_claim_an_active_topology(self):
        assignments = ["left_low", "left_high", "right_low", "right_high"]
        topology = derive_topology("stereo", assignments, channels=2, crossover_enabled=True)
        self.assertEqual(topology.roles, ("left_low", "left_high"))
        self.assertTrue(topology.issues)
        self.assertEqual(assignments[-1], "right_high")

    def test_invalid_roles_and_capacity_are_not_silently_coerced(self):
        for mode, assignments, channels, crossover in (
            ("surround", [], None, False), ("stereo-sub", ["left_low"], None, False),
            ("stereo", ["main_l"], None, True), ("stereo-sub", [True], None, False),
            ("stereo-sub", "main_l", None, False), ("stereo-sub", ["sub3"], None, False),
            ("stereo", ["sub1"], None, False),
            ("stereo-sub", [], True, False), ("stereo-sub", [], -1, False), ("stereo-sub", [], 33, False),
            ("stereo-sub", ["off"] * 33, None, False),
        ):
            with self.subTest(mode=mode, assignments=assignments, channels=channels):
                with self.assertRaises(ValueError):
                    derive_topology(mode, assignments, channels=channels, crossover_enabled=crossover)


if __name__ == "__main__":
    unittest.main()
