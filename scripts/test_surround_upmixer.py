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
HQ = {"upmixer": {"enabled": True, "params": {"mode": "hq", "center": 8, "surround": 4, "fftSize": 4096}}}


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

    def test_strengths_are_whole_numbers_from_zero_to_ten(self):
        for name in ("center", "surround"):
            for value in (0, 1, 5, 10, "7", "0", 7.0, 0.0):
                with self.subTest(name=name, value=value):
                    extras = self.manager.normalize_effects_extras({"upmixer": {"params": {name: value}}})
                    self.assertEqual(extras["upmixer"]["params"][name], int(float(value)))
            for value in (-1, 11, 2.5, "x", None, float("nan"), float("inf")):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.manager.normalize_effects_extras({"upmixer": {"params": {name: value}}})

    def test_zero_strength_survives_parse_merge_and_persistence(self):
        body = {"upmixerEnabled": True, "upmixerCenter": 0, "upmixer_surround": "0"}
        parsed = effects_extras.parse_effects_extras_from_json(body)
        self.assertEqual((parsed["upmixer"]["params"]["center"], parsed["upmixer"]["params"]["surround"]), (0.0, 0.0))
        merged = effects_extras.merge_effects_extras_from_json(self.manager.load_global_extras(), body)
        saved = self.manager.save_global_extras(merged)
        self.assertEqual(saved["upmixer"]["params"]["center"], 0)
        self.assertEqual(saved["upmixer"]["params"]["surround"], 0)
        # Missing, null and empty values keep the defaults; they are not 0.
        for value in (None, ""):
            fallback = effects_extras.parse_effects_extras_from_json({"upmixerCenter": value})
            self.assertEqual(fallback["upmixer"]["params"]["center"], 6)

    def test_mode_and_fft_size_are_validated_and_unknown_params_dropped(self):
        for params in ({"mode": "other"}, {"mode": "hq2"}, {"fftSize": 512}, {"fftSize": 16384}, {"fftSize": "x"}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.manager.normalize_effects_extras({"upmixer": {"params": params}})
        extras = self.manager.normalize_effects_extras(
            {"upmixer": {"enabled": True, "params": {"mode": " FAST ", "fftSize": 4096, "junk": 1}}})
        self.assertEqual(extras["upmixer"]["params"], {"mode": "fast", "center": 6, "surround": 6, "fftSize": 4096})
        for size in (1024, 2048, 4096, 8192):
            extras = self.manager.normalize_effects_extras({"upmixer": {"params": {"mode": " HQ ", "fftSize": str(size)}}})
            self.assertEqual(extras["upmixer"]["params"]["mode"], "hq")
            self.assertEqual(extras["upmixer"]["params"]["fftSize"], size)

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

    def test_hq_mode_and_fft_size_survive_parse_merge_and_persistence(self):
        body = {"upmixerEnabled": True, "upmixerMode": "hq", "upmixer_fft_size": "1024", "upmixerCenter": 0}
        merged = effects_extras.merge_effects_extras_from_json(self.manager.load_global_extras(), body)
        saved = self.manager.save_global_extras(merged)
        self.assertEqual(saved["upmixer"], {
            "enabled": True, "params": {"mode": "hq", "center": 0, "surround": 6, "fftSize": 1024}})
        # A later save that names only a strength keeps the engine and its size.
        merged = effects_extras.merge_effects_extras_from_json(saved, {"upmixerSurround": 3})
        again = self.manager.save_global_extras(merged)
        self.assertEqual(again["upmixer"]["params"], {"mode": "hq", "center": 0, "surround": 3, "fftSize": 1024})

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

    def test_hq_stage_names_the_engine_and_its_fft_size(self):
        self.assertEqual(stage_lines(self.compile(ROLES, HQ)), [
            "stage_begin 0 global-upmixer native upmixer", "param mode hq", "param fft_size 4096",
            "param center 8", "param surround 4",
            "param center_out 1", "param side_out 1", "param rear_out 1", "stage_end"])
        # The Fast engine has no transform size: it never reaches the engine config.
        self.assertNotIn("fft_size", "\n".join(stage_lines(self.compile(ROLES, ENABLED))))

    def test_hq_keeps_surround_extraction_without_a_center_and_with_zero_strengths(self):
        for center, surround in ((0, 6), (6, 0), (0, 0)):
            with self.subTest(center=center, surround=surround):
                extras = {"upmixer": {"enabled": True, "params": {"mode": "hq", "center": center, "surround": surround}}}
                lines = stage_lines(self.compile(QUAD_SUBS, extras))
                self.assertTrue(lines, "a missing Center or a zero strength must not drop the HQ upmixer")
                self.assertIn("param mode hq", lines)
                self.assertIn("param fft_size 2048", lines)
                self.assertIn("param center_out 0", lines)
                self.assertIn("param rear_out 1", lines)

    def test_front_rear_and_subs_without_center_keeps_surround_extraction(self):
        # The test host layout: Front L/R + Rear L/R + Sub 1/2, no Center, no Side.
        lines = stage_lines(self.compile(QUAD_SUBS, ENABLED))
        self.assertTrue(lines, "a missing Center must not drop the upmixer")
        self.assertIn("param center_out 0", lines)
        self.assertIn("param side_out 0", lines)
        self.assertIn("param rear_out 1", lines)
        self.assertIn("param surround 4", lines)

    def test_either_strength_may_be_zero_without_dropping_the_stage(self):
        # Center and Surround are independent: 0 only silences that share. The stage
        # (and with it the fixed latency) stays whichever of them is 0.
        for center, surround in ((0, 6), (6, 0), (0, 0)):
            for roles, center_out in ((ROLES, 1), (QUAD_SUBS, 0)):
                with self.subTest(center=center, surround=surround, center_out=center_out):
                    extras = {"upmixer": {"enabled": True, "params": {"center": center, "surround": surround}}}
                    lines = stage_lines(self.compile(roles, extras))
                    self.assertTrue(lines, "a zero strength must not drop the upmixer")
                    self.assertIn(f"param center {center}", lines)
                    self.assertIn(f"param surround {surround}", lines)
                    self.assertIn("param rear_out 1", lines)
                    self.assertIn(f"param center_out {center_out}", lines)

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

    def test_zero_strength_is_a_live_value_change_with_the_same_shape(self):
        before = self.compile(ROLES, ENABLED)
        for params, changed_names in (({"center": 0, "surround": 4}, {"center"}),
                                      ({"center": 8, "surround": 0}, {"surround"}),
                                      ({"center": 0, "surround": 0}, {"center", "surround"})):
            after = self.compile(ROLES, {"upmixer": {"enabled": True, "params": params}})
            old_shape, old_values = DSPRuntime._live_config(before)
            new_shape, new_values = DSPRuntime._live_config(after)
            with self.subTest(params=params):
                self.assertEqual(old_shape, new_shape)
                changed = {value[:3] for value in new_values if value not in old_values}
                self.assertEqual(changed, {("param", "global-upmixer", name) for name in changed_names})

    def test_hq_strength_changes_are_live_but_engine_and_fft_size_rebuild(self):
        def parts(extras):
            return DSPRuntime._live_config(self.compile(ROLES, extras))

        def hq(**params):
            return {"upmixer": {"enabled": True, "params": {"mode": "hq", "center": 8, "surround": 4, "fftSize": 2048, **params}}}

        base_shape, base_values = parts(hq())
        shape, values = parts(hq(center=2, surround=9))
        self.assertEqual(shape, base_shape)
        self.assertEqual({value[:3] for value in values if value not in base_values},
                         {("param", "global-upmixer", "center"), ("param", "global-upmixer", "surround")})
        # A new transform size cannot change live: the shape differs, so the runtime swaps the engine.
        for size in (1024, 4096):
            self.assertNotEqual(parts(hq(fftSize=size))[0], base_shape)
        # So does a change of the engine itself, either way.
        self.assertNotEqual(parts(ENABLED)[0], base_shape)
        self.assertNotEqual(parts({"upmixer": {"enabled": True, "params": {"mode": "fast", "center": 8, "surround": 4}}})[0], base_shape)

    def test_enabling_the_upmixer_changes_the_stage_shape(self):
        old_shape, _ = DSPRuntime._live_config(self.compile(QUAD_SUBS, {}))
        new_shape, _ = DSPRuntime._live_config(self.compile(QUAD_SUBS, ENABLED))
        self.assertNotEqual(old_shape, new_shape)


if __name__ == "__main__":
    unittest.main()
