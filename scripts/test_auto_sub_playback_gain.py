#!/usr/bin/env python3
"""Focused regressions for AutoSub raw-helper playback gain parity."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from measurement import MEASUREMENT_SUBWOOFER_HELPER_ROUTE, MeasurementStore


def runtime_config() -> main.SubwooferRuntimeConfig:
    return main.SubwooferRuntimeConfig(
        output_mode=main.OUTPUT_MODE_SUBWOOFER_21,
        output_key="test-output",
        output_label="Test output",
        output_channels=4,
        sample_rate=48000,
        crossover_frequency_hz=80,
        main_highpass_enabled=True,
        sub_level_db=0.0,
        sub_alignment_ms=0.0,
        sub_polarity="normal",
    )


class AutoSubPlaybackGainTests(unittest.TestCase):
    def test_loudness_off_uses_unity_and_does_not_touch_hardware_volume(self):
        manager = SimpleNamespace(load_global_extras=lambda: {
            "loudness": {"enabled": False, "params": {"volumeDb": -20.0}},
        })
        with patch.object(main, "easyeffects_manager", manager), patch.object(
            main, "set_output_volume"
        ) as set_output_volume:
            captured = main._capture_auto_sub_playback_gain()

        self.assertEqual(captured["linear"], 1.0)
        self.assertEqual(captured["volume_db"], 0.0)
        self.assertEqual(captured["source"], "hardware-sink")
        set_output_volume.assert_not_called()

        command = MeasurementStore._build_measurement_play_command(
            play_node_name="fxroute-measure-play-test",
            playback_path=Path("/tmp/sweep.wav"),
            playback_target={"target_name": "alsa_output.test"},
            playback_route={"route": MEASUREMENT_SUBWOOFER_HELPER_ROUTE},
            playback_gain=captured["linear"],
        )
        self.assertIn("--volume=1", command)

    def test_loudness_minus_twenty_uses_point_one_for_raw_helper(self):
        manager = SimpleNamespace(load_global_extras=lambda: {
            "loudness": {"enabled": True, "params": {"volumeDb": -20.0}},
        })
        with patch.object(main, "easyeffects_manager", manager):
            captured = main._capture_auto_sub_playback_gain()

        self.assertAlmostEqual(captured["linear"], 0.1, places=12)
        self.assertEqual(captured["volume_db"], -20.0)
        self.assertEqual(captured["source"], "loudness.params.volumeDb")

        command = MeasurementStore._build_measurement_play_command(
            play_node_name="fxroute-measure-play-test",
            playback_path=Path("/tmp/sweep.wav"),
            playback_target={"target_name": "alsa_output.test"},
            playback_route={"route": MEASUREMENT_SUBWOOFER_HELPER_ROUTE},
            playback_gain=captured["linear"],
        )
        self.assertIn("--volume=0.1", command)
        self.assertEqual(command[-1], "/tmp/sweep.wav")

    def test_stage_peak_prediction_uses_the_same_source_gain(self):
        profile = {
            "sweep_start_hz": 20.0,
            "sweep_end_hz": 600.0,
            "sweep_seconds": 0.1,
            "tail_seconds": 0.1,
        }
        full = main._auto_sub_stage_peak_prediction(
            sweep_profile=profile,
            sample_rate=48000,
            channel="stereo",
            config=runtime_config(),
            playback_gain=1.0,
        )
        quiet = main._auto_sub_stage_peak_prediction(
            sweep_profile=profile,
            sample_rate=48000,
            channel="stereo",
            config=runtime_config(),
            playback_gain=0.1,
        )

        self.assertEqual(quiet["playback_gain"], 0.1)
        for key, value in full["linear"].items():
            self.assertAlmostEqual(quiet["linear"][key], value * 0.1, places=12)

    def test_active_chain_ignores_raw_helper_gain_and_keeps_command_unchanged(self):
        kwargs = {
            "play_node_name": "fxroute-measure-play-test",
            "playback_path": Path("/tmp/sweep.wav"),
            "playback_target": {"target_name": "alsa_output.test"},
            "playback_route": {"route": "subwoofer-active-chain"},
        }
        without_gain = MeasurementStore._build_measurement_play_command(**kwargs)
        with_gain = MeasurementStore._build_measurement_play_command(
            **kwargs,
            playback_gain=0.1,
        )

        self.assertEqual(with_gain, without_gain)
        self.assertNotIn("--volume=", with_gain)

    def test_old_jobs_default_to_unity_gain(self):
        self.assertEqual(main._auto_sub_job_playback_gain({}), 1.0)
        self.assertAlmostEqual(
            main._auto_sub_job_playback_gain({"playback_gain": {"linear": 0.1}}),
            0.1,
        )


if __name__ == "__main__":
    unittest.main()
