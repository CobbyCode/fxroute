#!/usr/bin/env python3
"""ACTIVE_CHAIN measurement playback-target resolution contracts.

ACTIVE_CHAIN must resolve to the active EasyEffects chain (easyeffects_sink)
in every output mode and fail closed when that chain is unavailable.  The
explicit raw/helper scope keeps resolving to the hardware sink.
"""

import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from measurement import (
    MEASUREMENT_SCOPE_ACTIVE_CHAIN,
    MEASUREMENT_SCOPE_RAW_HELPER,
    MeasurementStore,
)


def stereo_overview():
    return {
        "output_mode": {"mode": "stereo"},
        "current_output": {"name": "alsa_output.hw"},
        "selected_output": {"target_name": "alsa_output.hw"},
        "default_output": {"target_name": "alsa_output.hw"},
    }


def subwoofer_overview():
    return {
        "output_mode": {"mode": "subwoofer-2.2"},
        "current_output": {"name": "alsa_output.hw"},
        "selected_output": {"target_name": "alsa_output.hw"},
        "default_output": {"target_name": "alsa_output.hw"},
    }


class MeasurementPlaybackTargetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MeasurementStore(home=pathlib.Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _resolve(self, overview, scope=MEASUREMENT_SCOPE_ACTIVE_CHAIN, ports_present=True):
        with patch("measurement.get_audio_output_overview", return_value=overview), patch.object(
            self.store, "_list_pw_ports", return_value=(
                ["easyeffects_sink:playback_FL", "easyeffects_sink:playback_FR"]
                if ports_present else []
            )
        ):
            return self.store._resolve_playback_target(measurement_scope=scope)

    def test_active_chain_resolves_to_easyeffects_sink_in_stereo(self):
        target = self._resolve(stereo_overview())
        self.assertEqual(target["target_name"], "easyeffects_sink")

    def test_active_chain_resolves_to_easyeffects_sink_in_subwoofer(self):
        target = self._resolve(subwoofer_overview())
        self.assertEqual(target["target_name"], "easyeffects_sink")

    def test_active_chain_fails_closed_when_easyeffects_sink_ports_missing(self):
        with self.assertRaises(RuntimeError) as caught:
            self._resolve(stereo_overview(), ports_present=False)
        self.assertIn("easyeffects_sink", str(caught.exception))
        self.assertNotIn("alsa_output.hw", str(caught.exception))

    def test_raw_helper_scope_keeps_hardware_sink(self):
        target = self._resolve(stereo_overview(), scope=MEASUREMENT_SCOPE_RAW_HELPER)
        self.assertEqual(target["target_name"], "alsa_output.hw")


if __name__ == "__main__":
    unittest.main()
