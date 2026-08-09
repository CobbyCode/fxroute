#!/usr/bin/env python3
"""Focused microphone-level QC, channel mapping and one-shot gain retry tests."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement import MeasurementStore


class MeasurementCaptureQualityTests(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.store = MeasurementStore(home=Path(self._home.name))

    def tearDown(self):
        self._home.cleanup()

    def quality_codes(self, peak_dbfs, rms_dbfs, *, audit_rms=-6.0):
        result = self.store._build_capture_quality_checks(
            capture_audit={"channels": 1, "rms_dbfs": audit_rms},
            timing={"start_score": 1.0, "end_score": 1.0, "drift_ppm": 0.0},
            peak_dbfs=peak_dbfs,
            rms_dbfs=rms_dbfs,
            trusted_band_meta={"stable_high_edge": True},
            trusted_max_hz=20_000.0,
            expect_dual_mono_channels=False,
        )
        return [item["code"] for item in result["items"]]

    def test_peak_at_low_threshold_produces_warning(self):
        self.assertIn("capture-level-low", self.quality_codes(-45.0, -59.0))

    def test_rms_at_low_threshold_produces_warning(self):
        self.assertIn("capture-level-low", self.quality_codes(-44.0, -60.0))

    def test_adequate_mic_level_has_no_low_warning(self):
        self.assertNotIn("capture-level-low", self.quality_codes(-44.9, -59.9))

    def test_selected_mic_levels_override_loud_reference_audit(self):
        self.assertIn("capture-level-low", self.quality_codes(-50.0, -70.0, audit_rms=-6.0))

    def test_recorded_channel_mapping_matches_capture_mode(self):
        self.assertEqual(
            self.store._recorded_mic_channel_index(3, has_electrical_reference=True),
            3,
        )
        self.assertEqual(
            self.store._recorded_mic_channel_index(3, has_electrical_reference=False),
            1,
        )

    def test_low_level_below_100_percent_retries_once(self):
        analysis = {"quality_checks": {"items": [
            {"level": "warning", "code": "capture-level-low", "message": "low"},
        ]}}
        with patch("measurement.get_node_volume", return_value=72), patch("measurement.set_node_volume") as set_volume:
            first = self.store._try_raise_mic_for_low_capture(
                analysis, mic_target="mic", attempt_index=0, mic_auto_boosted=False,
            )
            second = self.store._try_raise_mic_for_low_capture(
                analysis, mic_target="mic", attempt_index=1, mic_auto_boosted=True,
            )

        self.assertTrue(first)
        self.assertFalse(second)
        set_volume.assert_called_once_with("mic", 100)

    def test_low_level_at_100_percent_does_not_change_gain(self):
        analysis = {"quality_checks": {"items": [
            {"level": "warning", "code": "capture-level-low", "message": "low"},
        ]}}
        with patch("measurement.get_node_volume", return_value=100), patch("measurement.set_node_volume") as set_volume:
            retry = self.store._try_raise_mic_for_low_capture(
                analysis, mic_target="mic", attempt_index=0, mic_auto_boosted=False,
            )

        self.assertFalse(retry)
        set_volume.assert_not_called()

    def test_existing_clipping_thresholds_are_unchanged(self):
        self.assertIn("capture-clipped", self.quality_codes(-0.2, -20.0))
        self.assertIn("capture-near-clipping", self.quality_codes(-1.0, -20.0))
        self.assertNotIn("capture-clipped", self.quality_codes(-1.0, -20.0))


if __name__ == "__main__":
    unittest.main()
