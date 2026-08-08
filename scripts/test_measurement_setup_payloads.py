#!/usr/bin/env python3
"""Verhaltenstests für die REFACTOR-004-Extraktion:

- measurement.normalize_measurement_optional_input_channel
- measurement.measurement_setup_settings_from_payload

sowie Wrapper-Parität gegen measurement_session._normalize_measurement_optional_input_channel
und measurement_session._measurement_setup_settings_from_payload.
"""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
import measurement_session
from measurement import (
    measurement_setup_settings_from_payload,
    normalize_measurement_optional_input_channel,
)


class NormalizeOptionalInputChannelTests(unittest.TestCase):
    def test_none_and_empty_return_empty(self):
        for value in (None, ""):
            self.assertEqual(normalize_measurement_optional_input_channel(value), "")

    def test_positive_int_normalized(self):
        self.assertEqual(normalize_measurement_optional_input_channel(3), "3")
        self.assertEqual(normalize_measurement_optional_input_channel(1), "1")
        self.assertEqual(normalize_measurement_optional_input_channel("12"), "12")

    def test_whitespace_stripped(self):
        self.assertEqual(normalize_measurement_optional_input_channel("  4  "), "4")

    def test_zero_and_negative_return_empty(self):
        for value in (0, -1, "0", "-2"):
            self.assertEqual(normalize_measurement_optional_input_channel(value), "")

    def test_invalid_values_return_empty(self):
        for value in ("abc", "3.5", 3.5, True, [1], {}):
            self.assertEqual(normalize_measurement_optional_input_channel(value), "")


class SetupSettingsFromPayloadTests(unittest.TestCase):
    def test_empty_payload_returns_defaults(self):
        self.assertEqual(
            measurement_setup_settings_from_payload({}),
            {
                "selectedInputId": "",
                "selectedMicInputChannel": "1",
                "selectedReferenceInputChannel": "",
            },
        )

    def test_measure_not_a_dict_treated_as_empty(self):
        for payload in ({"measure": "x"}, {"measure": [1, 2]}, {"measure": None}):
            self.assertEqual(
                measurement_setup_settings_from_payload(payload),
                {
                    "selectedInputId": "",
                    "selectedMicInputChannel": "1",
                    "selectedReferenceInputChannel": "",
                },
            )

    def test_camel_case_keys_normalized(self):
        payload = {
            "measure": {
                "selectedInputId": "mic-1",
                "selectedMicInputChannel": 3,
                "selectedReferenceInputChannel": "2",
            }
        }
        self.assertEqual(
            measurement_setup_settings_from_payload(payload),
            {
                "selectedInputId": "mic-1",
                "selectedMicInputChannel": "3",
                "selectedReferenceInputChannel": "2",
            },
        )

    def test_snake_case_reference_key_fallback(self):
        # reference_input_channel wird nur als Fallback genutzt, wenn
        # selectedReferenceInputChannel fehlt bzw. None ist.
        payload = {"measure": {"selectedReferenceInputChannel": "4", "reference_input_channel": "9"}}
        result = measurement_setup_settings_from_payload(payload)
        self.assertEqual(result["selectedReferenceInputChannel"], "4")

        payload = {"measure": {"reference_input_channel": "9"}}
        result = measurement_setup_settings_from_payload(payload)
        self.assertEqual(result["selectedReferenceInputChannel"], "9")

        payload = {"measure": {"selectedReferenceInputChannel": None, "reference_input_channel": "9"}}
        result = measurement_setup_settings_from_payload(payload)
        self.assertEqual(result["selectedReferenceInputChannel"], "9")

    def test_invalid_channel_values_handled(self):
        payload = {
            "measure": {
                "selectedMicInputChannel": 0,
                "selectedReferenceInputChannel": "abc",
            }
        }
        result = measurement_setup_settings_from_payload(payload)
        self.assertEqual(result["selectedMicInputChannel"], "1")  # Default-Fallback
        self.assertEqual(result["selectedReferenceInputChannel"], "")

    def test_input_payload_not_mutated(self):
        payload = {"measure": {"selectedInputId": "x", "selectedMicInputChannel": 2}}
        before = copy.deepcopy(payload)
        measurement_setup_settings_from_payload(payload)
        self.assertEqual(payload, before)

    def test_selected_input_id_stringified(self):
        result = measurement_setup_settings_from_payload({"measure": {"selectedInputId": 42}})
        self.assertEqual(result["selectedInputId"], "42")
        result = measurement_setup_settings_from_payload({"measure": {"selectedInputId": None}})
        self.assertEqual(result["selectedInputId"], "")


class WrapperParityTests(unittest.TestCase):
    def test_normalize_wrapper_matches_module_function(self):
        for value in (None, "", " 3 ", 3, 0, -1, "abc", 1, "0", " 12 ", 3.5, True):
            self.assertEqual(
                measurement_session._normalize_measurement_optional_input_channel(value),
                normalize_measurement_optional_input_channel(value),
                f"Parität für {value!r}",
            )

    def test_settings_wrapper_matches_module_function(self):
        payloads = [
            {},
            {"measure": {}},
            {"measure": {"selectedInputId": "mic1", "selectedMicInputChannel": 3, "selectedReferenceInputChannel": "2"}},
            {"measure": {"input_id": "x", "mic_input_channel": "4", "reference_input_channel": "5"}},
            {"measure": {"selectedReferenceInputChannel": None, "reference_input_channel": "7"}},
            {"measure": "not-a-dict"},
            {"measure": {"selectedMicInputChannel": None, "selectedReferenceInputChannel": 0}},
        ]
        for payload in payloads:
            self.assertEqual(
                measurement_session._measurement_setup_settings_from_payload(payload),
                measurement_setup_settings_from_payload(payload),
                f"Parität für {payload!r}",
            )


if __name__ == "__main__":
    unittest.main()
