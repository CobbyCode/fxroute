#!/usr/bin/env python3
"""Verhaltenstests für die REFACTOR-010-Extraktion:

- effects_extras.parse_effects_extras_from_json
- effects_extras.merge_effects_extras_from_json
- effects_extras.is_pure_loudness_strength_change
- effects_extras.is_runtime_autogain_loudness_change

sowie Wrapper-Parität gegen main._parse_effects_extras_from_json,
main._merge_effects_extras_from_json, main._is_pure_loudness_strength_change
und main._is_runtime_autogain_loudness_change.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import effects_extras


def full_extras(**overrides) -> dict:
    """Baseline like parse_effects_extras_from_json produces."""
    base = {
        "limiter": {"enabled": False},
        "headroom": {"enabled": False, "params": {"gainDb": -3.0}},
        "autogain": {"enabled": False, "params": {"targetDb": -12.0}},
        "loudness": {
            "enabled": False,
            "params": {
                "fftSize": 4096,
                "strength": 10,
                "volumeDb": 0.0,
                "calibration": {},
                "calibrationProfiles": {},
            },
        },
        "delay": {"enabled": False, "params": {"leftMs": 0.0, "rightMs": 0.0}},
        "bass_enhancer": {
            "enabled": False,
            "params": {"amount": 0.0, "harmonics": 8.5, "scope": 100.0, "blend": 0.0},
        },
        "tone_effect": {"enabled": False, "mode": "crystalizer"},
    }
    base.update(overrides)
    return base


class ParseAliasTests(unittest.TestCase):
    def test_empty_body_uses_all_defaults(self):
        self.assertEqual(effects_extras.parse_effects_extras_from_json({}), full_extras())

    def test_camel_case_has_priority_over_snake_case(self):
        body = {"limiterEnabled": True, "limiter_enabled": False}
        parsed = effects_extras.parse_effects_extras_from_json(body)
        self.assertIs(parsed["limiter"]["enabled"], True)

        body = {"headroom_gain_db": -6.0, "headroomGainDb": -5.0}
        parsed = effects_extras.parse_effects_extras_from_json(body)
        self.assertEqual(parsed["headroom"]["params"]["gainDb"], -5.0)

        body = {"loudness_strength": "7", "loudnessStrength": "9"}
        parsed = effects_extras.parse_effects_extras_from_json(body)
        self.assertEqual(parsed["loudness"]["params"]["strength"], "9")

    def test_snake_case_accepted_when_camel_case_missing(self):
        body = {
            "limiter_enabled": True,
            "headroom_gain_db": -6.0,
            "autogain_target_db": -18.0,
            "loudness_fft_size": 2048,
            "loudness_strength": "7",
            "loudness_volume_db": -1.5,
            "delay_left_ms": 2.0,
            "delay_right_ms": 3.0,
            "bass_amount": 4.0,
            "tone_effect_mode": "BASS_BOOST",
        }
        parsed = effects_extras.parse_effects_extras_from_json(body)
        self.assertIs(parsed["limiter"]["enabled"], True)
        self.assertEqual(parsed["headroom"]["params"]["gainDb"], -6.0)
        self.assertEqual(parsed["autogain"]["params"]["targetDb"], -18.0)
        self.assertEqual(parsed["loudness"]["params"]["fftSize"], 2048)
        self.assertEqual(parsed["loudness"]["params"]["strength"], "7")
        self.assertEqual(parsed["loudness"]["params"]["volumeDb"], -1.5)
        self.assertEqual(parsed["delay"]["params"]["leftMs"], 2.0)
        self.assertEqual(parsed["delay"]["params"]["rightMs"], 3.0)
        self.assertEqual(parsed["bass_enhancer"]["params"]["amount"], 4.0)
        self.assertEqual(parsed["tone_effect"]["mode"], "bass_boost")

    def test_tone_effect_mode_normalized_only_on_parse(self):
        parsed = effects_extras.parse_effects_extras_from_json({"toneEffectMode": "  CRYSTALIZER  "})
        self.assertEqual(parsed["tone_effect"]["mode"], "crystalizer")
        parsed = effects_extras.parse_effects_extras_from_json({"tone_effect_mode": "bass_booster"})
        self.assertEqual(parsed["tone_effect"]["mode"], "bass_booster")


class ParseDefaultsAndNullTests(unittest.TestCase):
    def test_null_values_fall_back_to_defaults(self):
        body = {
            "limiterEnabled": None,
            "headroomGainDb": None,
            "autogainTargetDb": None,
            "loudnessFftSize": None,
            "loudnessVolumeDb": None,
            "delayLeftMs": None,
            "bassAmount": None,
            "toneEffectMode": None,
        }
        parsed = effects_extras.parse_effects_extras_from_json(body)
        self.assertEqual(parsed, full_extras())

    def test_empty_string_values_fall_back_to_defaults(self):
        body = {
            "headroomGainDb": "",
            "autogainTargetDb": "",
            "loudnessFftSize": "",
            "loudnessVolumeDb": "",
            "delayLeftMs": "",
            "bassAmount": "",
            "toneEffectMode": "",
        }
        parsed = effects_extras.parse_effects_extras_from_json(body)
        self.assertEqual(parsed["headroom"]["params"]["gainDb"], -3.0)
        self.assertEqual(parsed["autogain"]["params"]["targetDb"], -12.0)
        self.assertEqual(parsed["loudness"]["params"]["fftSize"], 4096)
        self.assertEqual(parsed["loudness"]["params"]["volumeDb"], 0.0)
        self.assertEqual(parsed["delay"]["params"]["leftMs"], 0.0)
        self.assertEqual(parsed["bass_enhancer"]["params"]["amount"], 0.0)
        self.assertEqual(parsed["tone_effect"]["mode"], "crystalizer")

    def test_zero_values_fall_back_to_defaults(self):
        # 0.0 / 0 sind falsy -> das bestehende `or`-Fallback greift
        body = {
            "headroomGainDb": 0.0,
            "autogainTargetDb": 0,
            "loudnessFftSize": 0,
            "loudnessVolumeDb": 0.0,
            "delayLeftMs": 0.0,
            "bassAmount": 0.0,
        }
        parsed = effects_extras.parse_effects_extras_from_json(body)
        self.assertEqual(parsed["headroom"]["params"]["gainDb"], -3.0)
        self.assertEqual(parsed["autogain"]["params"]["targetDb"], -12.0)
        self.assertEqual(parsed["loudness"]["params"]["fftSize"], 4096)
        self.assertEqual(parsed["loudness"]["params"]["volumeDb"], 0.0)
        self.assertEqual(parsed["delay"]["params"]["leftMs"], 0.0)
        self.assertEqual(parsed["bass_enhancer"]["params"]["amount"], 0.0)

    def test_strength_kept_as_is_no_or_fallback(self):
        # strength hat KEIN `or`-Fallback: 0 bleibt 0
        parsed = effects_extras.parse_effects_extras_from_json({"loudnessStrength": 0})
        self.assertEqual(parsed["loudness"]["params"]["strength"], 0)
        parsed = effects_extras.parse_effects_extras_from_json({"loudness_strength": None})
        self.assertIsNone(parsed["loudness"]["params"]["strength"])

    def test_boolean_conversion_from_strings(self):
        parsed = effects_extras.parse_effects_extras_from_json(
            {"loudnessEnabled": "false", "autogainEnabled": 1, "delayEnabled": "yes"}
        )
        # Nicht-leere Strings sind truthy -> bool() ergibt True
        self.assertIs(parsed["loudness"]["enabled"], True)
        self.assertIs(parsed["autogain"]["enabled"], True)
        self.assertIs(parsed["delay"]["enabled"], True)

    def test_numeric_conversion_from_strings(self):
        parsed = effects_extras.parse_effects_extras_from_json(
            {"headroomGainDb": "-4.5", "loudnessFftSize": "2048", "bassAmount": "3.5"}
        )
        self.assertEqual(parsed["headroom"]["params"]["gainDb"], -4.5)
        self.assertEqual(parsed["loudness"]["params"]["fftSize"], 2048)
        self.assertEqual(parsed["bass_enhancer"]["params"]["amount"], 3.5)


class ParseInvalidConversionTests(unittest.TestCase):
    def test_invalid_float_raises_value_error(self):
        with self.assertRaises(ValueError):
            effects_extras.parse_effects_extras_from_json({"headroomGainDb": "abc"})

    def test_invalid_int_raises_value_error(self):
        with self.assertRaises(ValueError):
            effects_extras.parse_effects_extras_from_json({"loudnessFftSize": "abc"})

    def test_none_value_falls_back_to_default(self):
        # None ist falsy -> das bestehende `or`-Fallback greift, kein TypeError
        parsed = effects_extras.parse_effects_extras_from_json({"headroomGainDb": None})
        self.assertEqual(parsed["headroom"]["params"]["gainDb"], -3.0)

    def test_truthy_unconvertible_object_raises_type_error(self):
        with self.assertRaises(TypeError):
            effects_extras.parse_effects_extras_from_json({"headroomGainDb": {"x": 1}})
        with self.assertRaises(TypeError):
            effects_extras.parse_effects_extras_from_json({"loudnessFftSize": {"x": 1}})


class ParseCalibrationTests(unittest.TestCase):
    def test_calibration_dict_passed_through(self):
        cal = {"points": [{"freq": 60, "gain": 2.0}]}
        parsed = effects_extras.parse_effects_extras_from_json({"calibration": cal})
        self.assertEqual(parsed["loudness"]["params"]["calibration"], cal)

    def test_calibration_non_dict_becomes_empty(self):
        for bad in ("x", 5, ["a"], True):
            parsed = effects_extras.parse_effects_extras_from_json({"calibration": bad})
            self.assertEqual(parsed["loudness"]["params"]["calibration"], {})

    def test_calibration_profiles_dict_passed_through(self):
        profiles = {"p1": {"name": "A"}}
        parsed = effects_extras.parse_effects_extras_from_json({"calibrationProfiles": profiles})
        self.assertEqual(parsed["loudness"]["params"]["calibrationProfiles"], profiles)

    def test_calibration_profiles_non_dict_becomes_empty(self):
        parsed = effects_extras.parse_effects_extras_from_json({"calibrationProfiles": 42})
        self.assertEqual(parsed["loudness"]["params"]["calibrationProfiles"], {})


class MergeBehaviorTests(unittest.TestCase):
    def test_empty_body_returns_deepcopy_unchanged(self):
        previous = full_extras()
        merged = effects_extras.merge_effects_extras_from_json(previous, {})
        self.assertEqual(merged, previous)
        self.assertIsNot(merged, previous)

    def test_only_explicitly_supplied_fields_applied(self):
        previous = full_extras(
            loudness={
                "enabled": True,
                "params": {"fftSize": 4096, "strength": "10", "volumeDb": 0.0, "calibration": {}, "calibrationProfiles": {}},
            },
            bass_enhancer={"enabled": True, "params": {"amount": 5.0, "harmonics": 8.5, "scope": 100.0, "blend": 0.0}},
        )
        merged = effects_extras.merge_effects_extras_from_json(previous, {"loudnessStrength": "14"})
        self.assertEqual(merged["loudness"]["params"]["strength"], "14")
        # Nicht gelieferte Felder bleiben unverändert
        self.assertEqual(merged["loudness"]["params"]["fftSize"], 4096)
        self.assertEqual(merged["bass_enhancer"]["params"]["amount"], 5.0)
        self.assertIs(merged["bass_enhancer"]["enabled"], True)

    def test_camel_case_priority_on_merge(self):
        previous = full_extras()
        merged = effects_extras.merge_effects_extras_from_json(
            previous, {"headroomGainDb": -5.0, "headroom_gain_db": -6.0}
        )
        self.assertEqual(merged["headroom"]["params"]["gainDb"], -5.0)

    def test_strength_converted_to_str_on_merge(self):
        previous = full_extras()
        merged = effects_extras.merge_effects_extras_from_json(previous, {"loudnessStrength": 14})
        self.assertIsInstance(merged["loudness"]["params"]["strength"], str)
        self.assertEqual(merged["loudness"]["params"]["strength"], "14")
        # str() wirft bei None keinen ValueError, ergibt "None" - Verhalten exakt bewahren
        merged = effects_extras.merge_effects_extras_from_json(previous, {"loudnessStrength": None})
        self.assertEqual(merged["loudness"]["params"]["strength"], "None")

    def test_tone_mode_not_normalized_on_merge(self):
        # Beim Merge wird NUR str() angewendet, kein strip/lower
        previous = full_extras()
        merged = effects_extras.merge_effects_extras_from_json(
            previous, {"toneEffectMode": "  BASS_BOOST  "}
        )
        self.assertEqual(merged["tone_effect"]["mode"], "  BASS_BOOST  ")

    def test_scalar_and_param_updates(self):
        previous = full_extras()
        body = {
            "limiterEnabled": True,
            "autogainTargetDb": -18.0,
            "loudnessFftSize": 2048,
            "delayLeftMs": 1.5,
            "bassAmount": 6.0,
            "toneEffectEnabled": True,
        }
        merged = effects_extras.merge_effects_extras_from_json(previous, body)
        self.assertIs(merged["limiter"]["enabled"], True)
        self.assertEqual(merged["autogain"]["params"]["targetDb"], -18.0)
        self.assertEqual(merged["loudness"]["params"]["fftSize"], 2048)
        self.assertEqual(merged["delay"]["params"]["leftMs"], 1.5)
        self.assertEqual(merged["bass_enhancer"]["params"]["amount"], 6.0)
        self.assertIs(merged["tone_effect"]["enabled"], True)

    def test_sections_created_when_missing(self):
        merged = effects_extras.merge_effects_extras_from_json({}, {"limiterEnabled": True})
        self.assertEqual(merged, {"limiter": {"enabled": True}})
        merged = effects_extras.merge_effects_extras_from_json({}, {"headroomGainDb": -4.0})
        self.assertEqual(merged, {"headroom": {"params": {"gainDb": -4.0}}})

    def test_invalid_float_on_merge_raises_value_error(self):
        with self.assertRaises(ValueError):
            effects_extras.merge_effects_extras_from_json(full_extras(), {"headroomGainDb": "abc"})


class MergeCalibrationValidationTests(unittest.TestCase):
    def test_calibration_must_be_object_exact_message(self):
        with self.assertRaises(ValueError) as ctx:
            effects_extras.merge_effects_extras_from_json(full_extras(), {"calibration": "x"})
        self.assertEqual(str(ctx.exception), "calibration must be an object")

    def test_calibration_profiles_must_be_object_exact_message(self):
        with self.assertRaises(ValueError) as ctx:
            effects_extras.merge_effects_extras_from_json(full_extras(), {"calibrationProfiles": 7})
        self.assertEqual(str(ctx.exception), "calibrationProfiles must be an object")

    def test_calibration_deepcopied_on_merge(self):
        previous = full_extras()
        cal = {"points": [{"freq": 60, "gain": 2.0}]}
        merged = effects_extras.merge_effects_extras_from_json(previous, {"calibration": cal})
        self.assertEqual(merged["loudness"]["params"]["calibration"], cal)
        # Nachträgliche Mutation des Inputs darf merged nicht verändern (Deepcopy)
        cal["points"][0]["gain"] = 99.0
        self.assertEqual(merged["loudness"]["params"]["calibration"]["points"][0]["gain"], 2.0)

    def test_calibration_profiles_deepcopied_on_merge(self):
        previous = full_extras()
        profiles = {"p1": {"name": "A"}}
        merged = effects_extras.merge_effects_extras_from_json(previous, {"calibrationProfiles": profiles})
        profiles["p1"]["name"] = "B"
        self.assertEqual(merged["loudness"]["params"]["calibrationProfiles"]["p1"]["name"], "A")


class NonMutationTests(unittest.TestCase):
    def test_parse_does_not_mutate_input(self):
        body = {
            "limiterEnabled": True,
            "headroomGainDb": -4.5,
            "loudness": {"enabled": True},
            "calibration": {"x": 1},
        }
        snapshot = {"limiterEnabled": True, "headroomGainDb": -4.5, "loudness": {"enabled": True}, "calibration": {"x": 1}}
        effects_extras.parse_effects_extras_from_json(body)
        self.assertEqual(body, snapshot)

    def test_merge_does_not_mutate_inputs(self):
        previous = full_extras()
        previous_snapshot = full_extras()
        body = {"loudnessStrength": "14", "calibration": {"x": 1}}
        body_snapshot = {"loudnessStrength": "14", "calibration": {"x": 1}}
        merged = effects_extras.merge_effects_extras_from_json(previous, body)
        self.assertEqual(previous, previous_snapshot)
        self.assertEqual(body, body_snapshot)
        # Mutating merged must not affect previous
        merged["loudness"]["params"]["strength"] = "99"
        self.assertEqual(previous["loudness"]["params"]["strength"], 10)

    def test_change_detection_does_not_mutate_inputs(self):
        previous = full_extras(loudness={"enabled": True, "params": {"strength": "10", "fftSize": 4096, "volumeDb": 0.0, "calibration": {}, "calibrationProfiles": {}}})
        current = full_extras(loudness={"enabled": True, "params": {"strength": "11", "fftSize": 4096, "volumeDb": 0.0, "calibration": {}, "calibrationProfiles": {}}})
        prev_snapshot = full_extras(loudness={"enabled": True, "params": {"strength": "10", "fftSize": 4096, "volumeDb": 0.0, "calibration": {}, "calibrationProfiles": {}}})
        curr_snapshot = full_extras(loudness={"enabled": True, "params": {"strength": "11", "fftSize": 4096, "volumeDb": 0.0, "calibration": {}, "calibrationProfiles": {}}})
        effects_extras.is_pure_loudness_strength_change(previous, current)
        effects_extras.is_runtime_autogain_loudness_change(previous, current)
        self.assertEqual(previous, prev_snapshot)
        self.assertEqual(current, curr_snapshot)


class PureStrengthChangeTests(unittest.TestCase):
    def _state(self, enabled=True, strength="10", fft_size=4096, volume_db=0.0, calibration=None, profiles=None):
        return {
            "loudness": {
                "enabled": enabled,
                "params": {
                    "fftSize": fft_size,
                    "strength": strength,
                    "volumeDb": volume_db,
                    "calibration": calibration or {},
                    "calibrationProfiles": profiles or {},
                },
            },
            "autogain": {"enabled": False, "params": {"targetDb": -12.0}},
            "headroom": {"enabled": False, "params": {"gainDb": -3.0}},
        }

    def test_strength_only_change_returns_true(self):
        self.assertTrue(
            effects_extras.is_pure_loudness_strength_change(
                self._state(strength="10"), self._state(strength="11")
            )
        )

    def test_strength_change_with_other_difference_returns_false(self):
        prev = self._state(strength="10")
        curr = self._state(strength="11", fft_size=2048)
        self.assertFalse(effects_extras.is_pure_loudness_strength_change(prev, curr))

    def test_no_strength_change_returns_false(self):
        self.assertFalse(
            effects_extras.is_pure_loudness_strength_change(
                self._state(strength="10"), self._state(strength="10")
            )
        )

    def test_loudness_disabled_returns_false(self):
        self.assertFalse(
            effects_extras.is_pure_loudness_strength_change(
                self._state(enabled=True, strength="10"),
                self._state(enabled=False, strength="11"),
            )
        )

    def test_missing_loudness_sections_handled(self):
        self.assertFalse(effects_extras.is_pure_loudness_strength_change({}, {}))
        self.assertFalse(effects_extras.is_pure_loudness_strength_change({"loudness": {}}, {"loudness": {}}))

    def test_strength_none_vs_value(self):
        self.assertTrue(
            effects_extras.is_pure_loudness_strength_change(
                self._state(strength=None), self._state(strength="10")
            )
        )


class RuntimeAutogainLoudnessChangeTests(unittest.TestCase):
    def _state(self, autogain_enabled=False, loudness_enabled=False, strength="10", target_db=-12.0):
        return {
            "autogain": {"enabled": autogain_enabled, "params": {"targetDb": target_db}},
            "loudness": {
                "enabled": loudness_enabled,
                "params": {
                    "fftSize": 4096,
                    "strength": strength,
                    "volumeDb": 0.0,
                    "calibration": {},
                    "calibrationProfiles": {},
                },
            },
            "headroom": {"enabled": False, "params": {"gainDb": -3.0}},
        }

    def test_autogain_change_returns_true(self):
        self.assertTrue(
            effects_extras.is_runtime_autogain_loudness_change(
                self._state(autogain_enabled=False),
                self._state(autogain_enabled=True),
            )
        )

    def test_loudness_change_returns_true(self):
        self.assertTrue(
            effects_extras.is_runtime_autogain_loudness_change(
                self._state(loudness_enabled=False),
                self._state(loudness_enabled=True),
            )
        )

    def test_strength_only_change_returns_true(self):
        # Der Vergleich umfasst den gesamten autogain-/loudness-Block inkl. params
        # (strength liegt in loudness.params) -> strength-only-Aenderung wird erkannt
        self.assertTrue(
            effects_extras.is_runtime_autogain_loudness_change(
                self._state(strength="10"), self._state(strength="11")
            )
        )

    def test_other_change_returns_false(self):
        prev = self._state()
        curr = self._state()
        curr["headroom"]["params"]["gainDb"] = -6.0
        self.assertFalse(effects_extras.is_runtime_autogain_loudness_change(prev, curr))

    def test_identical_returns_false(self):
        self.assertFalse(
            effects_extras.is_runtime_autogain_loudness_change(
                self._state(), self._state()
            )
        )

    def test_missing_sections_handled(self):
        self.assertFalse(effects_extras.is_runtime_autogain_loudness_change({}, {}))
        self.assertFalse(effects_extras.is_runtime_autogain_loudness_change({"x": 1}, {"x": 1}))


class WrapperParityTests(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main

    def test_parse_parity(self):
        bodies = [
            {},
            {"limiterEnabled": True, "limiter_enabled": False},
            {"headroomGainDb": -4.5, "autogain_target_db": -18, "loudnessEnabled": True, "loudnessStrength": "13"},
            {"calibration": {"x": 1}, "calibrationProfiles": {"p": {"y": 2}}, "toneEffectMode": "  BASS_BOOST  "},
            {"headroomGainDb": 0.0, "loudnessFftSize": 0, "loudnessStrength": 0},
            {"delayLeftMs": "1.5", "bassAmount": 3},
        ]
        for body in bodies:
            self.assertEqual(
                self.main._parse_effects_extras_from_json(body),
                effects_extras.parse_effects_extras_from_json(body),
            )

    def test_parse_parity_invalid_conversions(self):
        for body in ({"headroomGainDb": "abc"}, {"loudnessFftSize": "abc"}, {"delayRightMs": None}):
            try:
                self.main._parse_effects_extras_from_json(body)
                main_exc = None
            except Exception as exc:
                main_exc = (type(exc), str(exc))
            try:
                effects_extras.parse_effects_extras_from_json(body)
                mod_exc = None
            except Exception as exc:
                mod_exc = (type(exc), str(exc))
            self.assertEqual(main_exc, mod_exc)

    def test_merge_parity(self):
        previous = full_extras()
        bodies = [
            {},
            {"loudnessStrength": "14"},
            {"headroomGainDb": -5.0, "headroom_gain_db": -6.0},
            {"toneEffectMode": "  BASS_BOOST  "},
            {"calibration": {"x": 1}},
            {"calibrationProfiles": {"p": {"y": 2}}},
            {"limiterEnabled": True, "bassAmount": 6.0, "delayLeftMs": 1.5, "loudnessFftSize": 2048},
            {"loudnessStrength": None},
        ]
        for body in bodies:
            self.assertEqual(
                self.main._merge_effects_extras_from_json(previous, body),
                effects_extras.merge_effects_extras_from_json(previous, body),
            )

    def test_merge_parity_calibration_error(self):
        previous = full_extras()
        for body in ({"calibration": "x"}, {"calibrationProfiles": 7}):
            try:
                self.main._merge_effects_extras_from_json(previous, body)
                main_exc = None
            except Exception as exc:
                main_exc = (type(exc), str(exc))
            try:
                effects_extras.merge_effects_extras_from_json(previous, body)
                mod_exc = None
            except Exception as exc:
                mod_exc = (type(exc), str(exc))
            self.assertEqual(main_exc, mod_exc)

    def test_merge_parity_invalid_conversion(self):
        previous = full_extras()
        body = {"headroomGainDb": "abc"}
        try:
            self.main._merge_effects_extras_from_json(previous, body)
            main_exc = None
        except Exception as exc:
            main_exc = (type(exc), str(exc))
        try:
            effects_extras.merge_effects_extras_from_json(previous, body)
            mod_exc = None
        except Exception as exc:
            mod_exc = (type(exc), str(exc))
        self.assertEqual(main_exc, mod_exc)

    def test_pure_strength_change_parity(self):
        states = [
            ({"loudness": {"enabled": True, "params": {"strength": "10"}}}, {"loudness": {"enabled": True, "params": {"strength": "11"}}}),
            ({}, {}),
            ({"loudness": {"enabled": False, "params": {"strength": "10"}}}, {"loudness": {"enabled": True, "params": {"strength": "11"}}}),
        ]
        for prev, curr in states:
            self.assertEqual(
                self.main._is_pure_loudness_strength_change(prev, curr),
                effects_extras.is_pure_loudness_strength_change(prev, curr),
            )

    def test_runtime_autogain_loudness_change_parity(self):
        states = [
            ({"autogain": {"enabled": False}, "loudness": {"enabled": False}, "x": 1}, {"autogain": {"enabled": True}, "loudness": {"enabled": False}, "x": 1}),
            ({"autogain": {"enabled": False}, "loudness": {"enabled": False}, "x": 1}, {"autogain": {"enabled": False}, "loudness": {"enabled": False}, "x": 2}),
            ({}, {}),
        ]
        for prev, curr in states:
            self.assertEqual(
                self.main._is_runtime_autogain_loudness_change(prev, curr),
                effects_extras.is_runtime_autogain_loudness_change(prev, curr),
            )


if __name__ == "__main__":
    unittest.main()
