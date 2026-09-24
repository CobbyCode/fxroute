#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Main-only AutoSub references name only sub slots that actually exist.

A 2.1 system maps one sub slot, so the reference sweeps must not claim
sub2: the candidate configurator rejects unknown slots and both reference
sweeps would fail with "Subwoofer config sync failed".
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.autosub import measurement as am


def completed_result(**overrides):
    result = {
        "delay_ms": 0.0,
        "name": "0.0",
        "points": [[100.0, -3.0], [200.0, -3.0]],
        "calibrated_points": [[100.0, -3.0], [200.0, -3.0]],
        "sweep_id": "sweep",
        "status": "completed",
        "exact_sub_mute": True,
    }
    result.update(overrides)
    return result


def capture_active_subs(mode, sub_role_map):
    captured = []

    async def fake_candidate(**kwargs):
        captured.append(kwargs)
        return completed_result()

    job = {"output_state_context": {"sub_role_map": sub_role_map}}
    snapshot = {"mode": mode,
                "subwoofer": {"crossover_frequency_hz": 70, "crossover_family": "butterworth",
                              "crossover_slope_db_oct": 36, "main_highpass_enabled": True,
                              "sub_alignment_ms": 0.0, "sub_level_db": 0.0,
                              "sub_polarity": "normal"},
                "subwoofers": {"sub1": {"alignment_ms": 0.0}, "sub2": {"alignment_ms": 0.0}}}
    with patch.object(am, "_measure_auto_sub_candidate", side_effect=fake_candidate), \
         patch.object(am, "_analyze_auto_sub_main_target_anchor",
                      return_value={"status": "skipped"}):
        asyncio.run(am._capture_auto_sub_main_references(
            job=job, fc=70, input_id="mic", mic_input_channel="1",
            reference_input_channel="", calibration_ref="", calibration_filename=None,
            calibration_bytes=None, auto_sub_rate=48000, output_mode=mode,
            original_config_snapshot=snapshot))
    job.pop("output_state_context", None)
    json.dumps(job, allow_nan=False)
    return [kwargs["active_subs"] for kwargs in captured]


class MainReferenceSlotTests(unittest.TestCase):
    def test_single_sub_reference_names_only_the_mapped_slot(self):
        active = capture_active_subs(am.OUTPUT_MODE_SUBWOOFER_21, {"sub1": "sub1"})
        self.assertEqual(active, [("sub1",), ("sub1",)])

    def test_dual_mono_reference_names_both_mapped_slots(self):
        active = capture_active_subs(am.OUTPUT_MODE_SUBWOOFER_22,
                                     {"sub1": "sub1", "sub2": "sub2"})
        self.assertEqual(active, [("sub1", "sub2"), ("sub1", "sub2")])

    def test_references_complete_and_report_both_sides(self):
        async def run():
            job = {"output_state_context": {"sub_role_map": {"sub1": "sub1"}}}
            snapshot = {"mode": am.OUTPUT_MODE_SUBWOOFER_21,
                        "subwoofer": {"crossover_frequency_hz": 70,
                                      "main_highpass_enabled": True,
                                      "sub_alignment_ms": 0.0, "sub_level_db": 0.0,
                                      "sub_polarity": "normal"},
                        "subwoofers": {"sub1": {"alignment_ms": 0.0},
                                       "sub2": {"alignment_ms": 0.0}}}
            with patch.object(am, "_measure_auto_sub_candidate",
                              side_effect=lambda **kwargs: completed_result()), \
                 patch.object(am, "_analyze_auto_sub_main_target_anchor",
                              return_value={"status": "skipped"}):
                await am._capture_auto_sub_main_references(
                    job=job, fc=70, input_id="mic", mic_input_channel="1",
                    reference_input_channel="", calibration_ref="", calibration_filename=None,
                    calibration_bytes=None, auto_sub_rate=48000,
                    output_mode=am.OUTPUT_MODE_SUBWOOFER_21,
                    original_config_snapshot=snapshot)
            return job["main_references"]
        references = asyncio.run(run())
        self.assertEqual(references["status"], "completed")
        self.assertEqual(references["left"]["status"], "completed")
        self.assertEqual(references["right"]["status"], "completed")
        self.assertNotIn("auto_gain", references)


if __name__ == "__main__":
    unittest.main()
