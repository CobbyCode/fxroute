#!/usr/bin/env python3
"""Replay real gate decisions through same-position capture validation."""

import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.capture_policy import MeasurementCapturePolicyRunner
from measurement.hybrid import analyze_direct_window


FIXTURE = json.loads((Path(__file__).parent / "fixtures/direct-gate-repeat-decisions.json").read_text())


class DirectGateRepeatabilityTests(unittest.TestCase):
    def run_pair(self, observations, *, cancel_after_first=False, single=False):
        calls = []

        def capture(**kwargs):
            analysis = deepcopy(observations[len(calls)])
            # Curve values are sentinels; only detector metadata is from real captures.
            analysis["direct_response"]["points"] = [[1000, len(calls) + 1]]
            calls.append(kwargs)
            return analysis, {"capture_number": len(calls)}, {"playback_number": len(calls)}

        runner = MeasurementCapturePolicyRunner(
            capture_attempt=capture,
            is_cancelled=lambda *_: cancel_after_first and bool(calls),
            evaluate_electrical_reference=lambda _: {"usable": True},
            should_keep_electrical_reference=lambda *_: False,
            mark_electrical_reference_usable=lambda *_a, **_kw: None,
            append_reference_fallback_warning=lambda *_: None,
            analysis_has_warning=lambda *_: False,
            try_raise_mic=lambda *_a, **_kw: False,
            cancel_aware_sleep=lambda *_: None,
            retry_sleep=lambda *_: None,
            should_retry_host_capture=lambda *_: False,
            max_attempts=3, retry_delay=0,
        )
        run = runner.run if single else runner.run_direct_pair
        result = run(
            job_id="direct-test", owner_job_id="direct-test",
            use_electrical_reference=False, electrical_reference=None,
            host_reference={}, capture_channels=2, electrical_reference_channel_index=None,
            mic_target="mic",
        )
        return result, calls

    def test_real_right_early_late_switch_is_rejected_in_either_order(self):
        for pair in [FIXTURE["right"], list(reversed(FIXTURE["right"]))]:
            with self.subTest(first=pair[0]["job_id"]):
                result, calls = self.run_pair(pair)
                direct = result.analysis["direct_response"]
                self.assertEqual(len(calls), 2)
                self.assertFalse(direct["usable"])
                self.assertEqual(direct["status"], "reflection-ambiguous")
                self.assertEqual(direct["points"], [])
                self.assertEqual(direct["direct_confidence"], 1.0)
                check = direct["repeatability"]
                self.assertEqual(check["status"], "ambiguous")
                self.assertGreater(check["spread_octaves"], 1.7)
                self.assertEqual(check["lower_limit_range_hz"], [237.6, 800.0])
                self.assertIn("238", direct["retry_reason"])
                self.assertIn("800", direct["retry_reason"])
                self.assertEqual(len(check["observations"]), 2)

    def test_real_left_pair_keeps_the_complete_shorter_gate_capture(self):
        for pair, selected in [(FIXTURE["left"], 1), (list(reversed(FIXTURE["left"])), 0)]:
            result, calls = self.run_pair(pair)
            direct = result.analysis["direct_response"]
            self.assertEqual(len(calls), 2)
            self.assertTrue(direct["usable"])
            self.assertEqual(direct["gated_direct_lower_limit_hz"], 720)
            self.assertEqual(direct["gate_end_index"], 528263)
            self.assertEqual(direct["points"], [[1000, selected + 1]])
            self.assertEqual(result.capture_info["capture_number"], selected + 1)
            self.assertEqual(result.playback_info["playback_number"], selected + 1)
            self.assertEqual(direct["repeatability"]["status"], "consistent")
            self.assertLess(direct["repeatability"]["spread_octaves"], 0.02)

    def test_identical_right_decisions_are_not_rejected_by_channel_or_frequency(self):
        for observation in FIXTURE["right"]:
            result, _ = self.run_pair([observation, observation])
            self.assertTrue(result.analysis["direct_response"]["usable"])

    def test_reference_fallback_or_invalid_gate_cannot_validate_a_pair(self):
        for change in ["reference", "unusable", "zero", "missing"]:
            pair = deepcopy(FIXTURE["left"])
            if change == "reference":
                pair[1]["reference_path"]["channel"] = "host-monitor"
            elif change == "unusable":
                pair[1]["direct_response"]["usable"] = False
            elif change == "zero":
                pair[1]["direct_response"]["gated_direct_lower_limit_hz"] = 0
            else:
                pair[1]["direct_response"].pop("gated_direct_lower_limit_hz")
            result, _ = self.run_pair(pair)
            self.assertFalse(result.analysis["direct_response"]["usable"], change)

    def test_cancellation_prevents_the_second_sweep(self):
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.run_pair(FIXTURE["left"], cancel_after_first=True)

    def test_single_capture_policy_is_unchanged(self):
        result, calls = self.run_pair(FIXTURE["right"], single=True)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("repeatability", result.analysis["direct_response"])

    def test_full_resolution_gate_segment_can_replay_the_detector(self):
        ir = np.zeros(4000)
        ir[500] = 1
        ir[740] = 0.2
        ir[1000] = 0.4
        original = analyze_direct_window(ir, 48000, 500, timing_metadata={"confidence": 0.7})
        segment = original["reflection_detection"]["ir_segment"]
        self.assertEqual(segment["sample_rate"], 48000)
        self.assertLessEqual(len(segment["samples"]), 1300)
        replay = analyze_direct_window(
            np.array(segment["samples"]), segment["sample_rate"],
            original["direct_arrival_index"] - segment["start_index"],
            timing_metadata={"confidence": original["direct_confidence"]},
        )
        self.assertEqual(replay["gated_direct_lower_limit_hz"], original["gated_direct_lower_limit_hz"])
        self.assertEqual(replay["first_reflection_index"] + segment["start_index"], original["first_reflection_index"])
        self.assertEqual(segment["samples"], ir[segment["start_index"]:segment["start_index"] + len(segment["samples"])].tolist())


if __name__ == "__main__":
    unittest.main()
