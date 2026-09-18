#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""AutoSub routing adapter: topology -> optimizer path and role-derived masks.

The optimizers must never read legacy mode strings or fixed output 3/4
indices again: the routing is the only topology authority, so the adapter
derives the optimizer path, the sub roles, and the engine output indices
from the derived topology alone.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio.output_topology import derive_topology
from measurement.autosub.roles import (
    autosub_topology_from_state,
    main_roles_for_side,
    optimizer_path,
    require_autosub_topology,
    sub_mute_mask,
    sub_output_indices,
    sub_roles_for_side,
)


def _crossover_routing(ways=("low", "high"), subs=("sub1",)):
    routing = ["off"] * 4
    routing[0] = "left_low" if "low" in ways else "left_high"
    if len(ways) > 1:
        routing[1] = "left_high" if "low" in ways else "left_mid"
    routing[2] = "right_low" if "low" in ways else "right_high"
    if len(ways) > 1:
        routing[3] = "right_high" if "low" in ways else "right_mid"
    return routing + list(subs)


class OptimizerPathTests(unittest.TestCase):
    def test_no_sub_is_unavailable(self):
        topology = derive_topology("stereo", ["main_l", "main_r"])
        self.assertEqual(optimizer_path(topology), "unavailable")

    def test_single_sub_is_single_sub_path(self):
        topology = derive_topology("stereo-sub", ["main_l", "main_r", "sub1"])
        self.assertEqual(optimizer_path(topology), "single-sub")

    def test_lone_sub_l_is_single_sub_not_stereo(self):
        topology = derive_topology("stereo-sub", ["main_l", "main_r", "sub_l"])
        self.assertEqual(optimizer_path(topology), "single-sub")

    def test_classic_sub_pair_is_dual_sub(self):
        topology = derive_topology("stereo-sub", ["main_l", "main_r", "sub1", "sub2"])
        self.assertEqual(optimizer_path(topology), "dual-sub")

    def test_mixed_sub1_and_sub_r_is_dual_sub(self):
        """Mixed Sub 1/R must use the dual-mono path, never a stereo split."""
        topology = derive_topology("stereo-sub", ["main_l", "main_r", "sub1", "sub_r"])
        self.assertEqual(optimizer_path(topology), "dual-sub")

    def test_true_stereo_pair_is_stereo_subs(self):
        topology = derive_topology("stereo-sub", ["main_l", "main_r", "sub_l", "sub_r"])
        self.assertEqual(optimizer_path(topology), "stereo-subs")

    def test_three_subs_are_rejected(self):
        topology = derive_topology("stereo-sub", ["main_l", "main_r", "sub1", "sub2", "sub_l"])
        with self.assertRaises(ValueError):
            require_autosub_topology(topology)


class RequireTopologyTests(unittest.TestCase):
    def test_incomplete_crossover_routing_is_rejected(self):
        topology = derive_topology("stereo", ["left_low", "left_high"], crossover_enabled=True)
        with self.assertRaises(ValueError):
            require_autosub_topology(topology)

    def test_activatable_mono_topology_passes(self):
        topology = derive_topology("stereo-sub", ["main_l", "main_r", "sub1"])
        self.assertIs(require_autosub_topology(topology), topology)

    def test_unavailable_sub_topology_is_rejected_for_autosub(self):
        topology = derive_topology("stereo-sub", ["main_l", "main_r"])
        with self.assertRaises(ValueError):
            require_autosub_topology(topology)


class RoleIndexTests(unittest.TestCase):
    def test_sub_output_indices_follow_plan_order(self):
        topology = derive_topology("stereo-sub", ["main_l", "main_r", "sub1"])
        self.assertEqual(sub_output_indices(topology), (2,))

    def test_dual_sub_indices(self):
        topology = derive_topology("stereo-sub", ["main_l", "main_r", "sub1", "sub2"])
        self.assertEqual(sub_output_indices(topology), (2, 3))

    def test_stereo_pair_indices(self):
        # Plan order is canonical (mains first, then ways, then subs).
        topology = derive_topology("stereo-sub", ["sub_l", "sub_r", "main_l", "main_r"])
        self.assertEqual(sub_output_indices(topology), (2, 3))

    def test_main_only_mute_mask_mutes_sub_outputs(self):
        """Main-only references mute exactly the sub outputs, not fixed 3/4."""
        topology = derive_topology("stereo-sub", ["main_l", "main_r", "sub1"])
        self.assertEqual(sub_mute_mask(topology), 1 << 2)

    def test_main_only_mute_mask_is_canonical_order(self):
        topology = derive_topology("stereo-sub", ["sub1", "main_l", "main_r"])
        self.assertEqual(topology.roles, ("main_l", "main_r", "sub1"))
        self.assertEqual(sub_mute_mask(topology), 1 << 2)

    def test_no_subs_mute_mask_mutes_nothing(self):
        topology = derive_topology("stereo-sub", ["main_l", "main_r"])
        self.assertEqual(sub_mute_mask(topology), 0)


class SideRoleTests(unittest.TestCase):
    def test_stereo_main_roles_are_main_l_r(self):
        topology = derive_topology("stereo-sub", ["main_l", "main_r", "sub1", "sub2"])
        self.assertEqual(main_roles_for_side(topology, "left"), ("main_l",))
        self.assertEqual(main_roles_for_side(topology, "right"), ("main_r",))

    def test_crossover_main_is_all_ways_of_the_side(self):
        """In Crossover, Main is the sum of all that speaker's ways."""
        topology = derive_topology(
            "stereo-sub",
            ["left_low", "left_mid", "left_high", "right_low", "right_mid",
             "right_high", "sub1"],
            crossover_enabled=True,
        )
        self.assertEqual(
            main_roles_for_side(topology, "left"),
            ("left_low", "left_mid", "left_high"),
        )
        self.assertEqual(
            main_roles_for_side(topology, "right"),
            ("right_low", "right_mid", "right_high"),
        )

    def test_crossover_main_roles_exclude_subs(self):
        topology = derive_topology(
            "stereo-sub", ["left_low", "left_high", "right_low", "right_high", "sub1"],
            crossover_enabled=True,
        )
        self.assertEqual(main_roles_for_side(topology, "left"), ("left_low", "left_high"))

    def test_stereo_sub_pair_sides(self):
        topology = derive_topology("stereo-sub", ["main_l", "main_r", "sub_l", "sub_r"])
        self.assertEqual(sub_roles_for_side(topology, "left"), ("sub_l",))
        self.assertEqual(sub_roles_for_side(topology, "right"), ("sub_r",))

    def test_mono_and_dual_mono_subs_excite_both_sides(self):
        topology = derive_topology("stereo-sub", ["main_l", "main_r", "sub1", "sub2"])
        self.assertEqual(sub_roles_for_side(topology, "left"), ("sub1", "sub2"))
        self.assertEqual(sub_roles_for_side(topology, "right"), ("sub1", "sub2"))


class StateAdapterTests(unittest.TestCase):
    def test_topology_from_committed_state(self):
        from audio.output_state import default_output_state, set_mode_routing, switch_mode
        state = default_output_state()
        state = switch_mode(set_mode_routing(state, "stereo-sub", "dev", ["main_l", "main_r", "sub1", "off"]),
                            "stereo-sub")
        topology = autosub_topology_from_state(state, output_key="dev", channels=3)
        self.assertEqual(optimizer_path(topology), "single-sub")
        self.assertEqual(sub_output_indices(topology), (2,))

    def test_topology_from_state_respects_channels(self):
        from audio.output_state import default_output_state, set_mode_routing, switch_mode
        state = default_output_state()
        state = switch_mode(set_mode_routing(state, "stereo-sub", "dev", ["main_l", "main_r", "sub1"]),
                            "stereo-sub")
        topology = autosub_topology_from_state(state, output_key="dev", channels=2)
        self.assertEqual(optimizer_path(topology), "unavailable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
