#!/usr/bin/env python3
"""Behavior tests for sample-rate payload helpers (REFACTOR-003).

The five stateless normalization functions moved from main.py to samplerate.py;
main.py keeps thin wrappers. Covers field priority, numeric edge cases, missing
levels, unchanged input dicts and wrapper parity.
"""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
import measurement_session
import samplerate


class SampleRatePolicyTests(unittest.TestCase):
    def test_fixed_policy_overrides_source_rate_and_auto_preserves_it(self):
        self.assertEqual(
            samplerate.effective_playback_rate(44100, {"mode": "fixed", "rate": 48000}),
            48000,
        )
        self.assertEqual(
            samplerate.effective_playback_rate(44100, {"mode": "auto", "rate": None}),
            44100,
        )

    def test_policy_persistence_defaults_to_auto_and_validates_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample-rate-policy.json"
            with patch.object(samplerate, "_sample_rate_policy_path", return_value=path):
                self.assertEqual(samplerate.load_sample_rate_policy(), {"mode": "auto", "rate": None})
                samplerate.persist_sample_rate_policy({"mode": "fixed", "rate": 768000})
                self.assertEqual(samplerate.load_sample_rate_policy(), {"mode": "fixed", "rate": 768000})
                with self.assertRaises(ValueError):
                    samplerate.normalize_sample_rate_policy("fixed", 32000)


class OutputRateCapabilityTests(unittest.TestCase):
    def test_range_capability_filters_complete_candidate_list(self):
        payload = """
        Prop: key Spa:Pod:Object:Param:Format:Audio:rate (65539), flags 00000000
          Choice: type Spa:Enum:Choice:Range, flags 00000000 28 4
            Int 44100
            Int 44100
            Int 192000
        Prop: key Spa:Pod:Object:Param:Format:Audio:channels (65540), flags 00000000
        """
        self.assertEqual(
            samplerate._parse_enum_format_supported_rates(payload),
            [44100, 48000, 88200, 96000, 176400, 192000],
        )

    def test_node_inventory_maps_pipewire_id_by_sink_name(self):
        payload = '''
id 55, type PipeWire:Interface:Node/3
    node.description = "DAC"
    node.name = "alsa_output.usb-DAC"
id 56, type PipeWire:Interface:Node/3
    node.name = "easyeffects_sink"
'''
        self.assertEqual(
            samplerate._parse_pw_node_ids(payload),
            {"alsa_output.usb-DAC": 55, "easyeffects_sink": 56},
        )


class OverviewSampleRateTests(unittest.TestCase):
    def test_priority_output_mode_over_selected_output_over_top_level(self):
        overview = {
            "output_mode": {"effective_output_rate": 48000},
            "selected_output": {"active_rate": 44100},
            "active_rate": 96000,
        }
        self.assertEqual(samplerate.overview_sample_rate(overview), 48000)

    def test_falls_back_to_selected_output_active_rate(self):
        overview = {
            "output_mode": {"effective_output_rate": 0},
            "selected_output": {"active_rate": 44100},
            "active_rate": 96000,
        }
        self.assertEqual(samplerate.overview_sample_rate(overview), 44100)

    def test_selected_output_can_come_from_current_output(self):
        overview = {"current_output": {"active_rate": 88200}}
        self.assertEqual(samplerate.overview_sample_rate(overview), 88200)

    def test_falls_back_to_top_level_active_rate(self):
        overview = {"active_rate": 96000}
        self.assertEqual(samplerate.overview_sample_rate(overview), 96000)

    def test_only_positive_ints_are_valid(self):
        # Floats, strings, zero and negatives must not match. Note: bool is an
        # int subclass in Python, so True passes isinstance(int) and > 0 in the
        # original implementation as well (parity, not a bug).
        for invalid in (48000.0, "48000", 0, -1):
            overview = {"output_mode": {"effective_output_rate": invalid}}
            self.assertIsNone(samplerate.overview_sample_rate(overview), invalid)

    def test_missing_levels_return_none(self):
        self.assertIsNone(samplerate.overview_sample_rate({}))
        self.assertIsNone(samplerate.overview_sample_rate(None))
        self.assertIsNone(samplerate.overview_sample_rate(["not", "a", "dict"]))


class AuthoritativeSampleRateTests(unittest.TestCase):
    def test_force_rate_wins_over_active_rate(self):
        status = {"force_rate": 48000, "active_rate": 44100}
        self.assertEqual(samplerate.authoritative_sample_rate(status), 48000)

    def test_active_rate_when_no_force_rate(self):
        status = {"active_rate": 96000}
        self.assertEqual(samplerate.authoritative_sample_rate(status), 96000)

    def test_only_positive_ints_are_valid(self):
        for invalid in (48000.0, "48000", 0, -1):
            self.assertIsNone(samplerate.authoritative_sample_rate({"force_rate": invalid}), invalid)
            self.assertIsNone(samplerate.authoritative_sample_rate({"active_rate": invalid}), invalid)

    def test_missing_status_returns_none(self):
        self.assertIsNone(samplerate.authoritative_sample_rate({}))
        self.assertIsNone(samplerate.authoritative_sample_rate(None))
        self.assertIsNone(samplerate.authoritative_sample_rate("nope"))


class HelperArgumentSampleRateTests(unittest.TestCase):
    def test_parses_rate_argument(self):
        snapshot = {"helper_args": ["--foo", "--rate", "48000", "--bar"]}
        self.assertEqual(samplerate.helper_argument_sample_rate(snapshot), 48000)

    def test_zero_and_negative_rates_are_invalid(self):
        for value in ("0", "-1"):
            snapshot = {"helper_args": ["--rate", value]}
            self.assertIsNone(samplerate.helper_argument_sample_rate(snapshot), value)

    def test_missing_or_invalid_argument_returns_none(self):
        self.assertIsNone(samplerate.helper_argument_sample_rate({"helper_args": ["--rate"]}))
        self.assertIsNone(samplerate.helper_argument_sample_rate({"helper_args": ["--rate", "abc"]}))
        self.assertIsNone(samplerate.helper_argument_sample_rate({"helper_args": ["--foo"]}))
        self.assertIsNone(samplerate.helper_argument_sample_rate({}))
        self.assertIsNone(samplerate.helper_argument_sample_rate(None))


class AudioOutputOverviewWithEffectiveRateTests(unittest.TestCase):
    def test_sets_rate_in_all_matching_levels(self):
        overview = {
            "output_mode": {"mode": "subwoofer-2.1"},
            "selected_output": {"key": "out1", "name": "Out 1"},
            "current_output": {"key": "out1", "name": "Out 1"},
            "extra": "kept",
        }
        result = samplerate.audio_output_overview_with_effective_rate(overview, 48000)
        self.assertEqual(result["output_mode"]["effective_output_rate"], 48000)
        self.assertEqual(result["selected_output"]["active_rate"], 48000)
        self.assertEqual(result["current_output"]["active_rate"], 48000)
        self.assertEqual(result["extra"], "kept")
        self.assertEqual(result["output_mode"]["mode"], "subwoofer-2.1")

    def test_current_output_only_updated_when_key_matches(self):
        overview = {
            "selected_output": {"key": "out1"},
            "current_output": {"key": "out2"},
        }
        result = samplerate.audio_output_overview_with_effective_rate(overview, 44100)
        self.assertNotIn("active_rate", result["current_output"])
        self.assertEqual(result["selected_output"]["active_rate"], 44100)

    def test_input_overview_is_not_mutated(self):
        overview = {
            "output_mode": {"mode": "stereo"},
            "selected_output": {"key": "out1"},
            "current_output": {"key": "out1"},
            "nested": {"deep": [1, 2, 3]},
        }
        before = copy.deepcopy(overview)
        samplerate.audio_output_overview_with_effective_rate(overview, 48000)
        self.assertEqual(overview, before)

    def test_missing_levels_are_created_or_kept(self):
        result = samplerate.audio_output_overview_with_effective_rate({}, 48000)
        self.assertEqual(result["output_mode"], {"effective_output_rate": 48000})
        self.assertIsNone(result["selected_output"])
        self.assertIsNone(result["current_output"])

        overview = {"selected_output": None, "current_output": None}
        result = samplerate.audio_output_overview_with_effective_rate(overview, 48000)
        self.assertIsNone(result["selected_output"])
        self.assertIsNone(result["current_output"])


class MeasurementHelperSnapshotSummaryTests(unittest.TestCase):
    def test_full_snapshot_maps_all_fields(self):
        snapshot = {
            "active": True,
            "helper_pid": 4242,
            "config": {
                "sample_rate": 48000,
                "sub_alignment_ms": 2.0,
                "derived_main_delay_ms": 1.5,
                "derived_sub_delay_ms": 2.5,
            },
            "stage": "measuring",
            "last_error": None,
        }
        self.assertEqual(
            samplerate.measurement_helper_snapshot_summary(snapshot),
            {
                "active": True,
                "helper_pid": 4242,
                "sample_rate": 48000,
                "sub_alignment_ms": 2.0,
                "main_delay_ms": 1.5,
                "sub_delay_ms": 2.5,
                "stage": "measuring",
                "last_error": None,
            },
        )

    def test_inactive_snapshot_and_missing_config(self):
        self.assertEqual(
            samplerate.measurement_helper_snapshot_summary({"active": False}),
            {
                "active": False,
                "helper_pid": None,
                "sample_rate": None,
                "sub_alignment_ms": None,
                "main_delay_ms": None,
                "sub_delay_ms": None,
                "stage": None,
                "last_error": None,
            },
        )
        self.assertEqual(
            samplerate.measurement_helper_snapshot_summary(None)["active"],
            False,
        )


class MainWrapperParityTests(unittest.TestCase):
    def test_coordinator_target_rate_uses_persisted_policy(self):
        track = {"sample_rate_hz": 44100}
        with patch.object(
            samplerate,
            "load_sample_rate_policy",
            return_value={"mode": "fixed", "rate": 48000},
        ):
            self.assertEqual(main._coordinator_target_rate("local", track), 48000)
        with patch.object(
            samplerate,
            "load_sample_rate_policy",
            return_value={"mode": "auto", "rate": None},
        ):
            self.assertEqual(main._coordinator_target_rate("local", track), 44100)

    def test_overview_sample_rate_wrapper_matches(self):
        cases = [
            {"output_mode": {"effective_output_rate": 48000}, "active_rate": 96000},
            {"selected_output": {"active_rate": 44100}},
            {"active_rate": 88200},
            {},
            None,
            {"output_mode": {"effective_output_rate": 0}, "active_rate": 96000},
        ]
        for overview in cases:
            self.assertEqual(
                main._overview_sample_rate(overview),
                samplerate.overview_sample_rate(overview),
            )

    def test_authoritative_sample_rate_wrapper_matches(self):
        for status in ({"force_rate": 48000, "active_rate": 44100}, {"active_rate": 96000}, {}, None):
            self.assertEqual(
                main._authoritative_sample_rate(status),
                samplerate.authoritative_sample_rate(status),
            )

    def test_helper_argument_sample_rate_wrapper_matches(self):
        for snapshot in (
            {"helper_args": ["--rate", "48000"]},
            {"helper_args": ["--rate"]},
            {"helper_args": ["--rate", "abc"]},
            {},
            None,
        ):
            self.assertEqual(
                main._helper_argument_sample_rate(snapshot),
                samplerate.helper_argument_sample_rate(snapshot),
            )

    def test_overview_with_rate_wrapper_matches(self):
        overview = {"selected_output": {"key": "out1"}, "current_output": {"key": "out1"}}
        self.assertEqual(
            main._audio_output_overview_with_effective_rate(overview, 48000),
            samplerate.audio_output_overview_with_effective_rate(overview, 48000),
        )

    def test_snapshot_summary_wrapper_matches(self):
        snapshot = {"active": True, "config": {"sample_rate": 48000}, "stage": "x"}
        self.assertEqual(
            measurement_session._measurement_helper_snapshot_summary(snapshot),
            samplerate.measurement_helper_snapshot_summary(snapshot),
        )
        self.assertEqual(
            measurement_session._measurement_helper_snapshot_summary(None),
            samplerate.measurement_helper_snapshot_summary(None),
        )


if __name__ == "__main__":
    unittest.main()
