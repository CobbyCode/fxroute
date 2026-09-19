#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""The measurement target provider must keep a single routed way isolated.

Regression: main._freeze_measurement_target remapped every bank through
resolve_bank, turning the per-way role "right_low" into the pair bank "low"
(measured left+right). Speaker Auto Alignment freezes one physical way per
sweep, so a role that is actively routed must keep its own target; area ids
still resolve to their owning bank.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from audio.output_state import default_output_state, set_crossover, set_mode_routing, switch_mode


def crossover_state():
    state = default_output_state()
    routes = [f"{side}_{way}" for side in ("right", "left") for way in ("high", "low")]
    routes += ["sub1", "left_low"]
    state = set_mode_routing(set_crossover(state, "stereo-sub", True), "stereo-sub", "dev", routes)
    state = switch_mode(state, "stereo-sub")
    state["revision"] = 7
    processing = state["modes"]["stereo-sub"]["processing"]
    for side in ("left", "right"):
        processing[f"{side}_low"]["lowpass"] = {
            "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000,
        }
        processing[f"{side}_high"]["highpass"] = {
            "family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000,
        }
    return state, len(routes)


class FakeService:
    def __init__(self, state):
        self._state = state

    def ensure_state(self):
        return self._state

    def fingerprint(self, state, *, output_key, channels, sample_rate_hz):
        assert state is self._state
        return f"fp-{output_key}-{channels}-{sample_rate_hz}"


class WayTargetTests(unittest.TestCase):
    def setUp(self):
        self.state, self.channels = crossover_state()
        overview = {"output_mode": {"effective_output_key": "dev",
                                    "effective_output_channels": self.channels},
                    "selected_output": {}}
        service = FakeService(self.state)
        self._service = patch.object(main, "get_output_service", return_value=service)
        self._overview = patch.object(main, "get_audio_output_overview", return_value=overview)
        self._service.start()
        self._overview.start()
        self.addCleanup(self._service.stop)
        self.addCleanup(self._overview.stop)

    def test_routed_way_keeps_single_way_target(self):
        target = main._freeze_measurement_target("right_low", 48000)
        self.assertEqual(target["bank_id"], "right_low")
        self.assertEqual(target["measured_roles"], ["right_low"])

    def test_pair_bank_still_measures_both_sides(self):
        target = main._freeze_measurement_target("low", 48000)
        self.assertEqual(target["bank_id"], "low")
        self.assertEqual(target["measured_roles"], ["left_low", "right_low"])

    def test_global_still_measures_everything(self):
        target = main._freeze_measurement_target("global", 48000)
        self.assertEqual(target["bank_id"], "global")
        self.assertEqual(target["measured_roles"], target["roles"])
        self.assertIn("right_low", target["measured_roles"])

    def test_unknown_bank_still_fails_closed(self):
        with self.assertRaises(ValueError):
            main._freeze_measurement_target("no-such-bank", 48000)


if __name__ == "__main__":
    unittest.main()
