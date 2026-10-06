#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Surround upmixer extras: schema, validation, engine stage and live updates."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dsp.effects_extras as effects_extras
from dsp.manager import DSPManager, UnsupportedPluginError
from dsp.native_config import layout_from_plan
from dsp.runtime import DSPRuntime
import test_surround as surround_support

QUAD_SUBS, ROLES = surround_support.QUAD_SUBS, surround_support.ROLES

ENABLED = {"upmixer": {"enabled": True, "params": {"mode": "fast", "center": 8, "surround": 4}}}


def stage_lines(text):
    """Lines of the upmixer stage, or [] when the config has none."""
    lines, inside = [], False
    for line in text.splitlines():
        if line.startswith("stage_begin") and "native upmixer" in line:
            inside = True
        if inside:
            lines.append(line)
        if inside and line == "stage_end":
            break
    return lines


class UpmixerExtrasTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = DSPManager(Path(self.directory.name) / "presets", Path(self.directory.name) / "irs")
        self.surround = surround_support.SurroundTests()

    def compile(self, roles, extras, preset="Neutral"):
        layout = layout_from_plan(self.surround.plan(self.surround.state(roles)), resolve_ir=lambda name: {})
        return self.manager.compile_engine_text(layout, preset_name=preset, extras_override=extras)

    # --- schema -----------------------------------------------------------

    def test_defaults_are_off_with_fast_engine_and_mid_strengths(self):
        extras = self.manager.normalize_effects_extras({})
        self.assertEqual(extras["upmixer"], {
            "enabled": False,
            "params": {"mode": "fast", "center": 6, "surround": 6, "fftSize": 2048}})

    def test_strengths_are_whole_numbers_from_one_to_ten(self):
        for value in (1, 5, 10, "7", 7.0):
            with self.subTest(value=value):
                extras = self.manager.normalize_effects_extras({"upmixer": {"params": {"center": value}}})
                self.assertEqual(extras["upmixer"]["params"]["center"], int(float(value)))
        for name in ("center", "surround"):
            for value in (0, -1, 11, 2.5, "x", None, float("nan"), float("inf")):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.manager.normalize_effects_extras({"upmixer": {"params": {name: value}}})

    def test_mode_and_fft_size_are_validated_and_unknown_params_dropped(self):
        for params in ({"mode": "hq"}, {"mode": "other"}, {"fftSize": 512}, {"fftSize": "x"}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.manager.normalize_effects_extras({"upmixer": {"params": params}})
        extras = self.manager.normalize_effects_extras(
            {"upmixer": {"enabled": True, "params": {"mode": " FAST ", "fftSize": 4096, "junk": 1}}})
        self.assertEqual(extras["upmixer"]["params"], {"mode": "fast", "center": 6, "surround": 6, "fftSize": 4096})

    def test_json_parse_merge_and_persistence_round_trip(self):
        body = {"upmixerEnabled": True, "upmixerCenter": 9, "upmixer_surround": "3", "upmixerMode": "fast"}
        parsed = effects_extras.parse_effects_extras_from_json(body)
        self.assertEqual(parsed["upmixer"]["params"]["center"], 9.0)
        merged = effects_extras.merge_effects_extras_from_json(self.manager.load_global_extras(), body)
        saved = self.manager.save_global_extras(merged)
        self.assertEqual(saved["upmixer"], {
            "enabled": True, "params": {"mode": "fast", "center": 9, "surround": 3, "fftSize": 2048}})
        reloaded = DSPManager(Path(self.directory.name) / "presets", Path(self.directory.name) / "irs")
        self.assertEqual(reloaded.load_global_extras()["upmixer"], saved["upmixer"])
        self.assertEqual(json.loads(reloaded.global_extras_file.read_text())["upmixer"], saved["upmixer"])

    def test_merge_without_upmixer_fields_keeps_the_stored_section(self):
        stored = self.manager.save_global_extras(ENABLED)
        merged = effects_extras.merge_effects_extras_from_json(stored, {"limiterEnabled": True})
        self.assertEqual(self.manager.normalize_effects_extras(merged)["upmixer"], stored["upmixer"])

    def test_presets_cannot_carry_an_upmixer_plugin(self):
        preset = {"schema": self.manager.PRESET_SCHEMA, "version": self.manager.PRESET_VERSION,
                  "chain": [{"id": "upmixer#0", "type": "upmixer", "enabled": True, "params": {}}],
                  "metadata": {}}
        with self.assertRaises((UnsupportedPluginError, ValueError)):
            self.manager.import_preset_json("Upmix.json", json.dumps(preset))

    # --- engine stage -----------------------------------------------------

    def test_full_surround_routing_extracts_to_every_destination(self):
        self.assertEqual(stage_lines(self.compile(ROLES, ENABLED)), [
            "stage_begin 0 global-upmixer native upmixer", "param mode fast",
            "param center 8", "param surround 4",
            "param center_out 1", "param side_out 1", "param rear_out 1", "stage_end"])

    def test_front_rear_and_subs_without_center_keeps_surround_extraction(self):
        # The test host layout: Front L/R + Rear L/R + Sub 1/2, no Center, no Side.
        lines = stage_lines(self.compile(QUAD_SUBS, ENABLED))
        self.assertTrue(lines, "a missing Center must not drop the upmixer")
        self.assertIn("param center_out 0", lines)
        self.assertIn("param side_out 0", lines)
        self.assertIn("param rear_out 1", lines)
        self.assertIn("param surround 4", lines)

    def test_side_only_and_center_only_routings_name_their_destinations(self):
        side = stage_lines(self.compile(["main_l", "main_r", "side_l", "side_r"], ENABLED))
        self.assertIn("param center_out 0", side)
        self.assertIn("param side_out 1", side)
        self.assertIn("param rear_out 0", side)
        center = stage_lines(self.compile(["main_l", "main_r", "center"], ENABLED))
        self.assertIn("param center_out 1", center)
        self.assertIn("param side_out 0", center)
        self.assertIn("param rear_out 0", center)

    def test_routing_without_any_destination_has_no_stage(self):
        self.assertEqual(stage_lines(self.compile(["main_l", "main_r", "sub1"], ENABLED)), [])

    def test_off_direct_stereo_and_measurement_extras_have_no_stage(self):
        self.assertEqual(stage_lines(self.compile(ROLES, {})), [])
        self.assertEqual(stage_lines(self.compile(ROLES, ENABLED, preset="Direct")), [])
        stereo = self.manager.compile_engine_text(
            [{"name": "main_l", "source": 0}, {"name": "main_r", "source": 1}],
            preset_name="Neutral", extras_override=ENABLED)
        self.assertNotIn("upmixer", stereo)
        # Measurement plans pass their own empty extras and excite single speakers.
        self.assertEqual(stage_lines(self.compile(ROLES, self.surround.plan(self.surround.state())["global"]["extras"])), [])

    def test_upmixer_is_the_first_stage_ahead_of_the_other_extras(self):
        extras = {**ENABLED, "tone_effect": {"enabled": True, "mode": "crystalizer"},
                  "limiter": {"enabled": True}}
        order = [line.split()[2] for line in self.compile(ROLES, extras).splitlines()
                 if line.startswith("stage_begin")]
        self.assertEqual(order, ["global-upmixer", "global-crystalizer", "global-limiter"])

    # --- live updates -----------------------------------------------------

    def test_strength_changes_are_live_value_changes_not_a_rebuild(self):
        before = self.compile(QUAD_SUBS, ENABLED)
        after = self.compile(QUAD_SUBS, {"upmixer": {"enabled": True, "params": {"center": 8, "surround": 9}}})
        old_shape, old_values = DSPRuntime._live_config(before)
        new_shape, new_values = DSPRuntime._live_config(after)
        self.assertEqual(old_shape, new_shape)
        changed = {value[:3] for value in new_values if value not in old_values}
        self.assertEqual(changed, {("param", "global-upmixer", "surround")})

    def test_enabling_the_upmixer_changes_the_stage_shape(self):
        old_shape, _ = DSPRuntime._live_config(self.compile(QUAD_SUBS, {}))
        new_shape, _ = DSPRuntime._live_config(self.compile(QUAD_SUBS, ENABLED))
        self.assertNotEqual(old_shape, new_shape)


if __name__ == "__main__":
    unittest.main()
