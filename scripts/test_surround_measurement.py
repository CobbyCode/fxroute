#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Surround sweeps excite position inputs through the existing masked DSP plan."""

from copy import deepcopy
from itertools import count
from pathlib import Path
from subprocess import CompletedProcess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import default_output_state, set_crossover, set_mode_routing, switch_mode
from dsp.native_config import layout_from_plan
from dsp.processing_plan import compile_processing_plan
from measurement.routing import MeasurementRouting
from measurement.target import freeze_measurement_target, target_output_mask


PLAY = "fxroute-measure-play-test"
SINK = "fxroute_dsp_sink"
ROLES = ["main_l", "main_r", "center", "side_l", "side_r", "rear_l", "rear_r", "sub1", "sub2"]
STEREO_LINKS = {(f"{PLAY}:output_FL", f"{SINK}:playback_FL"),
                (f"{PLAY}:output_FR", f"{SINK}:playback_FR")}


class PipeWireFixture:
    """Replace only external port discovery and link commands with a live graph."""

    def __init__(self, layout, mode="surround", mask=0):
        self.runtime = {"active": True, "effect_bypass": True, "output_mask": mask,
                        "config": {"sample_rate": 48000, "output_mode": mode,
                                   "layout": deepcopy(layout), "plan_fingerprint": "frozen-plan"}}
        self.ports = {SINK: [f"{SINK}:playback_FL", f"{SINK}:playback_FR",
                             f"{SINK}:monitor_FL", f"{SINK}:monitor_FR"],
                      "fxroute_dsp": [f"fxroute_dsp:input_{index}" for index in range(1, 9)],
                      PLAY: [f"{PLAY}:output_FL", f"{PLAY}:output_FR"]}
        self.permanent = {(f"{SINK}:monitor_FL", "fxroute_dsp:input_1"),
                          (f"{SINK}:monitor_FR", "fxroute_dsp:input_2"),
                          ("fxroute_dsp:output_1", "hardware:playback_AUX0")}
        self.links = set(self.permanent)
        self.phantom_target = None
        self.failed_target = None
        self.runtime_snapshot_provider = lambda: deepcopy(self.runtime)
        self._routing_command_runner = self.run
        self._routing_output_overview = lambda: {"output_mode": {"mode": "stereo"}}

    @staticmethod
    def _normalize_measurement_scope(scope):
        if scope not in {"active_chain", "raw_helper"}:
            raise ValueError("Invalid measurement scope")
        return scope

    def _list_pw_ports(self, node):
        return list(self.ports.get(node, []))

    def _create_pipewire_link(self, source, target):
        if target == self.failed_target:
            raise RuntimeError("Link creation failed")
        if target != self.phantom_target:
            self.links.add((source, target))

    def _disconnect_link(self, source, target):
        existed = (source, target) in self.links
        self.links.discard((source, target))
        return existed

    def run(self, command, **kwargs):
        if command != ["pw-link", "-l"]:
            raise AssertionError(f"Unexpected external command: {command}")
        listing = "\n".join(f"{source}\n  |-> {target}" for source, target in sorted(self.links))
        return CompletedProcess(command, 0, listing, "")


class SurroundMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.state = switch_mode(set_mode_routing(default_output_state(), "surround", "device", ROLES), "surround")
        self.plan = self.compile(self.state)
        self.layout = layout_from_plan(self.plan, resolve_ir=lambda _: self.fail("Unexpected IR"))
        self.clock = count(0.0, 0.1)
        self.sleep = patch("measurement.routing.time.sleep", return_value=None)
        self.monotonic = patch("measurement.routing.time.monotonic", side_effect=lambda: next(self.clock))
        self.sleep.start()
        self.monotonic.start()
        self.addCleanup(self.sleep.stop)
        self.addCleanup(self.monotonic.stop)

    @staticmethod
    def compile(state):
        return compile_processing_plan(state, output_key="device", channels=16, sample_rate_hz=48000,
                                       preset_loader=lambda _: {"chain": []})

    def route(self, bank="all", *, layout=None, scope="active_chain", mode="surround", channel="stereo", group=None):
        layout = self.layout if layout is None else layout
        target = freeze_measurement_target(self.state, bank_id=bank, output_key="device", channels=16,
                                          sample_rate_hz=48000, fingerprint="frozen-plan", group=group)
        mask = target_output_mask(target, roles=target["roles"]) if layout is self.layout else 0
        fixture = PipeWireFixture(layout, mode, mask)
        routing = MeasurementRouting(fixture)
        playback_target = {"target_name": SINK}
        routing._resolve_host_reference_capture(playback_target=playback_target,
            mic_source_node_name="microphone", requested_channel=channel)
        route = routing._build_measurement_playback_route(PLAY, playback_target, measurement_scope=scope,
            expected_native_layout=layout, expected_native_output_mode=mode, expected_plan_fingerprint="frozen-plan")
        return fixture, routing, route

    def link(self, fixture, routing, route):
        return routing._link_measurement_playback_to_direct_sink(play_node_name=PLAY,
            playback_target={"target_name": SINK}, playback_route=route)

    def assert_links(self, fixture, extra):
        self.assertEqual(fixture.links - fixture.permanent, STEREO_LINKS | set(extra))

    def test_center_uses_one_mono_input_without_doubling_or_lfe(self):
        fixture, routing, route = self.route("center")
        diagnostics = self.link(fixture, routing, route)
        self.assert_links(fixture, {(f"{PLAY}:output_FL", "fxroute_dsp:input_3")})
        self.assertEqual(len(diagnostics["active_chain_input_links"]), 3)
        self.assertEqual(route["expected_native_layout"], self.layout)

    def test_side_and_rear_pairs_keep_left_right_positions(self):
        for bank, left, right in (("side", 5, 6), ("rear", 7, 8)):
            with self.subTest(bank=bank):
                fixture, routing, route = self.route(bank)
                self.link(fixture, routing, route)
                self.assert_links(fixture, {(f"{PLAY}:output_FL", f"fxroute_dsp:input_{left}"),
                                            (f"{PLAY}:output_FR", f"fxroute_dsp:input_{right}")})

    def test_right_only_sweep_excites_center_from_right_without_a_left_sum(self):
        fixture, routing, route = self.route("center", channel="right")
        self.link(fixture, routing, route)
        self.assert_links(fixture, {(f"{PLAY}:output_FR", "fxroute_dsp:input_3")})

    def test_one_side_mask_does_not_excite_opposite_surround_bass_sends(self):
        fixture = PipeWireFixture(self.layout, mask=0b111010111)
        routing = MeasurementRouting(fixture)
        route = routing._build_measurement_playback_route(PLAY, {"target_name": SINK},
            expected_native_layout=self.layout, expected_native_output_mode="surround",
            expected_plan_fingerprint="frozen-plan")
        self.link(fixture, routing, route)
        self.assert_links(fixture, {(f"{PLAY}:output_FL", "fxroute_dsp:input_5"),
                                    (f"{PLAY}:output_FL", "fxroute_dsp:input_7")})

    def test_global_excites_every_speaker_position_but_not_lfe(self):
        fixture, routing, route = self.route()
        self.link(fixture, routing, route)
        self.assert_links(fixture, {(f"{PLAY}:output_FL", "fxroute_dsp:input_3"),
                                    (f"{PLAY}:output_FL", "fxroute_dsp:input_5"),
                                    (f"{PLAY}:output_FR", "fxroute_dsp:input_6"),
                                    (f"{PLAY}:output_FL", "fxroute_dsp:input_7"),
                                    (f"{PLAY}:output_FR", "fxroute_dsp:input_8")})

    def test_all_banks_group_sweeps_excite_only_the_selected_positions(self):
        for group, extra in (
                ("front", set()),
                ("center", {(f"{PLAY}:output_FL", "fxroute_dsp:input_3")}),
                ("side", {(f"{PLAY}:output_FL", "fxroute_dsp:input_5"),
                          (f"{PLAY}:output_FR", "fxroute_dsp:input_6")}),
                ("rear", {(f"{PLAY}:output_FL", "fxroute_dsp:input_7"),
                          (f"{PLAY}:output_FR", "fxroute_dsp:input_8")})):
            with self.subTest(group=group):
                fixture, routing, route = self.route(group=group)
                self.link(fixture, routing, route)
                self.assert_links(fixture, extra)

    def test_all_banks_center_group_keeps_the_requested_reference_side(self):
        for channel, side in (("left", "FL"), ("right", "FR")):
            with self.subTest(channel=channel):
                fixture, routing, route = self.route(group="center", channel=channel)
                self.link(fixture, routing, route)
                self.assert_links(fixture, {(f"{PLAY}:output_{side}", "fxroute_dsp:input_3")})

    def test_fold_down_sends_add_no_sweep_excitation(self):
        roles = ["main_l", "main_r", "rear_l", "rear_r", "sub1", "sub2"]
        self.state = switch_mode(set_mode_routing(default_output_state(), "surround", "device", roles), "surround")
        self.plan = self.compile(self.state)
        self.layout = layout_from_plan(self.plan, resolve_ir=lambda _: self.fail("Unexpected IR"))
        fixture, routing, route = self.route("rear")
        self.link(fixture, routing, route)
        self.assert_links(fixture, {(f"{PLAY}:output_FL", "fxroute_dsp:input_7"),
                                    (f"{PLAY}:output_FR", "fxroute_dsp:input_8")})
        fixture, routing, route = self.route("main")
        self.link(fixture, routing, route)
        self.assert_links(fixture, set())

    def test_sub_measurement_uses_only_existing_shared_lr_bus(self):
        for bank in ("sub1", "sub2"):
            with self.subTest(bank=bank):
                fixture, routing, route = self.route(bank)
                self.link(fixture, routing, route)
                self.assert_links(fixture, set())

    def test_front_crossover_uses_unchanged_stereo_ingress(self):
        self.state = set_crossover(self.state, "surround", True)
        self.state = set_mode_routing(self.state, "surround", "device",
            ["left_low", "left_high", "right_low", "right_high", *ROLES[2:]])
        self.plan = self.compile(self.state)
        self.layout = layout_from_plan(self.plan, resolve_ir=lambda _: self.fail("Unexpected IR"))
        fixture, routing, route = self.route("high")
        self.link(fixture, routing, route)
        self.assert_links(fixture, set())

    def test_bank_only_layout_routes_are_not_confused_with_output_indices(self):
        for name, channel, side in (("center", 2, "FL"), ("side_l", 4, "FL"),
                                    ("side_r", 5, "FR"), ("rear_l", 6, "FL"), ("rear_r", 7, "FR")):
            with self.subTest(name=name):
                layout = [{"name": name, "routes": [{"input": channel, "gain": 1.0}]}]
                fixture, routing, route = self.route(layout=layout)
                self.link(fixture, routing, route)
                self.assert_links(fixture, {(f"{PLAY}:output_{side}", f"fxroute_dsp:input_{channel + 1}")})

    def test_raw_autosub_full_plan_keeps_the_front_lr_bus_only(self):
        fixture, routing, route = self.route(scope="raw_helper")
        self.link(fixture, routing, route)
        self.assert_links(fixture, set())

    def test_raw_bank_only_center_and_rear_follow_position_routes(self):
        for name, channel, side in (("center", 2, "FR"), ("rear_r", 7, "FR")):
            with self.subTest(name=name):
                layout = [{"name": name, "routes": [{"input": channel, "gain": 1.0}]}]
                fixture, routing, route = self.route(layout=layout, scope="raw_helper", channel="right")
                self.link(fixture, routing, route)
                self.assert_links(fixture, {(f"{PLAY}:output_{side}", f"fxroute_dsp:input_{channel + 1}")})

    def test_legacy_bank_source_selects_its_canonical_input(self):
        fixture, routing, route = self.route(layout=[{"name": "side_r", "source": 5}])
        self.link(fixture, routing, route)
        self.assert_links(fixture, {(f"{PLAY}:output_FR", "fxroute_dsp:input_6")})

    def test_mask_selection_is_frozen_with_the_playback_route(self):
        fixture, routing, route = self.route("center")
        fixture.runtime["output_mask"] = 0
        fixture.runtime["config"]["layout"][2]["routes"][0]["input"] = 7
        self.link(fixture, routing, route)
        self.assert_links(fixture, {(f"{PLAY}:output_FL", "fxroute_dsp:input_3")})

    def test_missing_position_port_fails_before_any_playback_link(self):
        fixture, routing, route = self.route("rear")
        fixture.ports["fxroute_dsp"].remove("fxroute_dsp:input_8")
        with self.assertRaisesRegex(RuntimeError, "input_8"):
            self.link(fixture, routing, route)
        self.assertEqual(fixture.links, fixture.permanent)

    def test_phantom_surround_link_fails_and_cleans_every_temporary_link(self):
        fixture, routing, route = self.route("side")
        fixture.phantom_target = "fxroute_dsp:input_6"
        with self.assertRaisesRegex(RuntimeError, "link"):
            self.link(fixture, routing, route)
        self.assertEqual(fixture.links, fixture.permanent)

    def test_failed_position_link_cleans_already_created_links(self):
        fixture, routing, route = self.route("rear")
        fixture.failed_target = "fxroute_dsp:input_8"
        with self.assertRaisesRegex(RuntimeError, "Link creation failed"):
            self.link(fixture, routing, route)
        self.assertEqual(fixture.links, fixture.permanent)

    def test_missing_stereo_reference_link_also_refuses_surround_playback(self):
        fixture, routing, route = self.route("center")
        fixture.phantom_target = f"{SINK}:playback_FR"
        with self.assertRaisesRegex(RuntimeError, "link"):
            self.link(fixture, routing, route)
        self.assertEqual(fixture.links, fixture.permanent)

    def test_cleanup_handles_tree_listing_without_link_metadata(self):
        fixture, routing, route = self.route("side")
        self.link(fixture, routing, route)
        removed = routing._cleanup_measurement_playback_links(play_node_name=PLAY)
        self.assertEqual(fixture.links, fixture.permanent)
        self.assertEqual(len(removed), 4)

    def test_stereo_routes_do_not_acquire_surround_inputs(self):
        layout = [{"name": "main_l", "routes": [{"input": 0, "gain": 1.0}]},
                  {"name": "main_r", "routes": [{"input": 1, "gain": 1.0}]}]
        fixture, routing, route = self.route(layout=layout, mode="stereo")
        self.link(fixture, routing, route)
        self.assert_links(fixture, set())

    def test_cleanup_does_not_remove_a_different_play_node_with_the_same_prefix(self):
        fixture, routing, route = self.route("center")
        other_link = (f"{PLAY}-other:output_FL", "fxroute_dsp:input_3")
        fixture.links.add(other_link)
        self.link(fixture, routing, route)
        routing._cleanup_measurement_playback_links(play_node_name=PLAY)
        self.assertEqual(fixture.links, fixture.permanent | {other_link})


if __name__ == "__main__":
    unittest.main()
