#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Isolated sub sweeps use their passband, not the broadband stopband, as zero."""

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import default_output_state, set_mode_routing, switch_mode
from measurement.store import MeasurementStore
from measurement.target import freeze_measurement_target
from test_measurement_bank_target import analysis_payload


def sub_payload(role="sub1"):
    # Bass is 40 dB above the old 120 Hz..8 kHz reference, like a low-passed sub.
    points = [[20, 34], [30, 36], [40, 40], [50, 52], [60, 44], [80, 42],
              [120, 32], [250, 15], [500, 5], [1000, 0], [2000, -2],
              [4000, -6], [8000, -10], [20000, -12]]
    return {
        "id": "isolated-sub", "measurement_kind": "sweep-response-v3",
        "channel": "stereo", "display": {"normalize": True},
        "traces": [{"kind": "sweep-response", "role": "trusted", "points": points}],
        "review_traces": [{"kind": "sweep-response-review", "role": "raw-review",
                           "points": [[10, 20], *copy.deepcopy(points)]}],
        "analysis": {"normalized_by_db": -70, "clock": {"band_limited_reference": False}},
        "measurement_target": {"bank_id": role, "measured_roles": [role]},
        "audio_output_context": {"crossover_frequency_hz": 80},
    }


class SubNormalizationTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        root = Path(home.name)
        environment = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_STATE_HOME": str(root / "state"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.store = MeasurementStore(home=root)

    def test_saved_subs_center_in_passband_and_preserve_calibrated_levels(self):
        for role in ("sub1", "sub2", "sub_l", "sub_r"):
            with self.subTest(role=role):
                original = sub_payload(role)
                before = copy.deepcopy(original)
                result = self.store._persistence._normalize_measurement(original)
                self.assertEqual(result["traces"][0]["points"][:6],
                                 [[20, -7], [30, -5], [40, -1], [50, 11], [60, 3], [80, 1]])
                self.assertEqual(result["analysis"]["normalized_by_db"], -29)
                self.assertEqual(result["analysis"]["level_reference_band_hz"], [20, 80])
                self.assertEqual(result["summary"]["max_db"], 11)
                self.assertEqual(result["review_summary"]["max_db"], 11)
                for key in ("traces", "review_traces"):
                    for old, new in zip(before[key][0]["points"], result[key][0]["points"]):
                        self.assertEqual(old[0], new[0])
                        self.assertAlmostEqual(old[1] - 70, new[1] - 29)
                self.assertEqual(original, before, "Reading must not mutate source data")

    def test_loading_legacy_sub_does_not_rewrite_file_and_saving_is_idempotent(self):
        original = sub_payload()
        source = self.store.measurements_dir / "isolated-sub.json"
        source.write_text(json.dumps(original))
        before = source.read_bytes()
        loaded = self.store.get_measurement("isolated-sub")
        self.assertLess(loaded["summary"]["max_db"], 24)
        self.assertEqual(source.read_bytes(), before)
        loaded["id"] = "saved-again"
        saved = self.store.save_measurement(loaded)
        reloaded = self.store.get_measurement("saved-again")
        reloaded.pop("storage_path")
        saved.pop("storage_path", None)
        self.assertEqual(reloaded, saved)
        self.assertEqual(saved["traces"], loaded["traces"])
        self.assertEqual(saved["analysis"], loaded["analysis"])

    def test_low_left_global_and_autosub_keep_their_existing_reference(self):
        for bank, roles, kind in (
            ("low", ["left_low", "right_low"], "sweep-response-v3"),
            ("global", ["main_l", "main_r", "sub1"], "sweep-response-v3"),
            ("sub1", ["sub1"], "auto_sub"),
        ):
            with self.subTest(bank=bank, kind=kind):
                original = sub_payload()
                original["measurement_kind"] = kind
                original["measurement_target"] = {"bank_id": bank, "measured_roles": roles}
                result = self.store._persistence._normalize_measurement(original)
                self.assertEqual(result["traces"][0]["points"], original["traces"][0]["points"])
                self.assertEqual(result["analysis"], original["analysis"])

    def test_missing_or_unusable_reference_does_not_guess_from_live_settings(self):
        for context in ({}, {"crossover_frequency_hz": 10}, {"crossover_frequency_hz": "invalid"}):
            original = sub_payload()
            original["audio_output_context"] = context
            result = self.store._persistence._normalize_measurement(original)
            self.assertEqual(result["traces"][0]["points"], original["traces"][0]["points"])
            self.assertEqual(result["analysis"], original["analysis"])

    def test_malformed_roles_do_not_hide_saved_measurements(self):
        for roles in (1, {"sub1": True}, "sub1"):
            with self.subTest(roles=roles):
                original = sub_payload()
                original["measurement_target"]["measured_roles"] = roles
                source = self.store.measurements_dir / "isolated-sub.json"
                source.write_text(json.dumps(original))
                listed = self.store.list_measurements()["measurements"]
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0]["analysis"], original["analysis"])

    def test_new_sub_result_uses_frozen_rendered_band_before_save(self):
        state = switch_mode(set_mode_routing(default_output_state(), "stereo-sub", "A",
                                            ["main_l", "main_r", "sub1", "sub2"]), "stereo-sub")
        state["modes"]["stereo-sub"]["bass_management"]["frequency_hz"] = 60
        target = freeze_measurement_target(state, bank_id="sub2", output_key="A", channels=4,
                                           sample_rate_hz=48000, fingerprint="frozen")
        # Later edits cannot choose the normalization range of this take.
        state["modes"]["stereo-sub"]["bass_management"]["frequency_hz"] = 200
        analysis = analysis_payload()
        analysis.update(normalized_by_db=-70,
                        trusted_points=sub_payload()["traces"][0]["points"],
                        review_points=sub_payload()["review_traces"][0]["points"])
        result = self.store._persistence._build_measurement_from_analysis(
            analysis, input_device={}, channel="stereo", calibration={}, measurement_target=target)
        self.assertEqual(result["traces"][0]["points"][3], [50, 12])
        self.assertEqual(result["analysis"]["level_reference_band_hz"], [20, 60])
        self.assertEqual(result["analysis"]["normalized_by_db"], -30)

    def test_stereo_sub_uses_its_own_filters_in_frozen_band(self):
        state = switch_mode(set_mode_routing(default_output_state(), "stereo-sub", "A",
                                            ["main_l", "main_r", "sub_l", "sub_r"]), "stereo-sub")
        config = state["modes"]["stereo-sub"]
        config["bass_management"].update(sub_link=False, sub_filters={
            "left": {"frequency_hz": 60, "family": "linkwitz-riley", "slope_db_oct": 24},
            "right": {"frequency_hz": 140, "family": "linkwitz-riley", "slope_db_oct": 24},
        })
        config["processing"]["sub_r"]["highpass"] = {
            "frequency_hz": 30, "family": "butterworth", "slope_db_oct": 12}
        for role, expected in (("sub_l", [20, 60]), ("sub_r", [30, 140])):
            target = freeze_measurement_target(state, bank_id=role, output_key="A", channels=4,
                                               sample_rate_hz=48000, fingerprint="frozen")
            self.assertEqual(target.get("level_reference_band_hz"), expected)


if __name__ == "__main__":
    unittest.main()
