#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Crossover way-response endpoint: backend-evaluated filter curves."""

import asyncio
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main
from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state import (default_bass_management, default_output_state,
                                set_bass_management, set_crossover, set_mode_routing,
                                switch_mode)
from audio.output_state_store import OutputStateStore
from dsp.manager import DSPManager


class FakeRequest:
    async def json(self):
        return {}


def make_service(directory):
    manager = DSPManager(home=Path(directory) / "home")
    return OutputService(OutputServiceDeps(
        store=OutputStateStore(Path(directory) / "output-state.json"),
        preset_loader=manager.preset_store.read,
        resolve_ir=lambda kernel: (_ for _ in ()).throw(AssertionError(kernel)),
        measurement_active=lambda: False,
    ))


def crossover_state(service):
    assignments = [f"{side}_{way}" for side in ("left", "right") for way in ("low", "mid", "high")]
    state = switch_mode(set_mode_routing(set_crossover(default_output_state(), "stereo-sub", True), "stereo-sub", "A", assignments),
                        "stereo-sub")
    for role, settings in state["modes"]["stereo-sub"]["processing"].items():
        if not role.startswith(("left_", "right_")):
            continue
        if not role.endswith("low"):
            settings["highpass"] = {"family": "linkwitz-riley", "slope_db_oct": 24,
                                    "frequency_hz": 300 if role.endswith("mid") else 2500}
        if not role.endswith("high"):
            settings["lowpass"] = {"family": "linkwitz-riley", "slope_db_oct": 24,
                                   "frequency_hz": 300 if role.endswith("low") else 2500}
    return service._deps.store.commit(state, expected_revision=0)


class WayResponseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.service = make_service(directory.name)
        crossover_state(self.service)

    def fetch(self, rate=48000):
        with mock.patch.multiple(
            main,
            get_output_service=mock.MagicMock(return_value=self.service),
            get_samplerate_status=mock.MagicMock(return_value={"active_rate": rate}),
            get_audio_output_overview=mock.MagicMock(return_value={
                "selected_output": {"key": "A", "channels": 6},
                "output_mode": {"effective_output_key": "A", "effective_output_channels": 6},
            }),
        ):
            return asyncio.run(main.get_audio_output_state_crossover_response())

    def test_lr24_cutoff_and_structure(self):
        payload = self.fetch()
        self.assertEqual(payload["mode"], "stereo-sub")
        self.assertTrue(payload["crossover_enabled"])
        self.assertEqual(payload["sample_rate_hz"], 48000)
        ways = payload["ways"]
        self.assertEqual(sorted(ways), ["left_high", "left_low", "left_mid",
                                        "right_high", "right_low", "right_mid"])
        low = ways["left_low"]
        self.assertTrue(low["complete"])
        points = dict(low["points"])
        self.assertGreater(len(points), 100)
        nearest = min(points, key=lambda hz: abs(math.log(hz / 300.0)))
        self.assertAlmostEqual(points[nearest], -6.0206, delta=0.15)
        self.assertAlmostEqual(points[min(points)], 0.0, delta=0.2)
        mid = ways["left_mid"]
        self.assertTrue(mid["complete"])
        self.assertEqual(mid["filters"]["highpass"]["frequency_hz"], 300)
        self.assertEqual(mid["filters"]["lowpass"]["frequency_hz"], 2500)

    def test_cleared_way_direction_reports_its_real_band(self):
        """Type Off is a valid state: the way draws the band it really runs."""
        state = self.service.load()
        state["modes"]["stereo-sub"]["processing"]["left_high"]["highpass"] = None
        self.service._deps.store.commit(state, expected_revision=1)
        payload = self.fetch()
        high = payload["ways"]["left_high"]
        self.assertFalse(high["complete"])
        self.assertIsNone(high["filters"]["highpass"])
        points = dict(high["points"])
        self.assertGreater(len(points), 100)
        # Without any filter the way runs flat across the whole range.
        self.assertAlmostEqual(points[min(points)], 0.0, delta=0.2)
        self.assertAlmostEqual(points[max(points)], 0.0, delta=0.2)
        # The way's own low-pass direction is untouched on the Low side.
        self.assertTrue(payload["ways"]["left_low"]["complete"])

    def test_stereo_mode_has_no_ways(self):
        from audio.output_state import set_crossover
        state = set_crossover(self.service.load(), "stereo-sub", False)
        self.service._deps.store.commit(state, expected_revision=1)
        payload = self.fetch()
        self.assertEqual(payload["mode"], "stereo-sub")
        self.assertFalse(payload["crossover_enabled"])
        self.assertEqual(payload["ways"], {})

    def _commit_sub_crossover_state(self, main_highpass_enabled=True, assignments=None, **bass):
        assignments = assignments or ["left_low", "right_low", "left_high", "right_high",
                                      "sub_l", "sub_r"]
        state = set_mode_routing(self.service.load(), "stereo-sub", "A", assignments)
        proc = state["modes"]["stereo-sub"]["processing"]
        for role in ("left_low", "right_low"):
            proc[role]["highpass"] = None
            proc[role]["lowpass"] = {"family": "linkwitz-riley", "slope_db_oct": 24,
                                     "frequency_hz": 2000}
        for role in ("left_high", "right_high"):
            proc[role]["highpass"] = {"family": "linkwitz-riley", "slope_db_oct": 24,
                                      "frequency_hz": 2000}
            proc[role]["lowpass"] = None
        options = {"frequency_hz": 80, "main_highpass_enabled": main_highpass_enabled}
        options.update(bass)
        state = set_bass_management(state, "stereo-sub", **options)
        return self.service._deps.store.commit(state, expected_revision=1)

    def test_sub_bass_highpass_shapes_low_way(self):
        self._commit_sub_crossover_state(main_highpass_enabled=True)
        payload = self.fetch()
        self.assertEqual(payload["bass_management"],
                         {**default_bass_management(), "frequency_hz": 80,
                          "main_highpass_enabled": True})
        self.assertEqual(sorted(payload["sub_roles"]), ["sub_l", "sub_r"])
        low = payload["ways"]["left_low"]
        self.assertTrue(low["complete"])
        self.assertEqual(low["derived_highpass"],
                         {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 80})
        self.assertIsNone(low["filters"]["highpass"])
        points = dict(low["points"])
        # LR24 high-pass at 80 Hz: the running low curve is deeply
        # attenuated at 20 Hz, unlike the sub-less ~0 dB bottom.
        self.assertLess(points[min(points)], -20.0)
        nearest = min(points, key=lambda hz: abs(math.log(hz / 80.0)))
        self.assertAlmostEqual(points[nearest], -6.0206, delta=0.6)

    def test_crossover_type_and_slope_shape_the_low_way(self):
        self._commit_sub_crossover_state(main_highpass_enabled=True,
                                         family="butterworth", slope_db_oct=12)
        payload = self.fetch()
        low = payload["ways"]["left_low"]
        self.assertEqual(low["derived_highpass"],
                         {"family": "butterworth", "slope_db_oct": 12, "frequency_hz": 80})
        points = dict(low["points"])
        nearest = min(points, key=lambda hz: abs(math.log(hz / 80.0)))
        # A 12 dB/oct Butterworth high-pass sits at -3 dB on its cutoff and
        # rolls off far more slowly than the LR24 it replaces.
        self.assertAlmostEqual(points[nearest], -3.0103, delta=0.6)
        # Two octaves below the cutoff a 12 dB/oct Butterworth is at ~-24 dB,
        # where the LR24 it replaces sits far deeper.
        self.assertAlmostEqual(points[min(points)], -24.1, delta=1.5)

    def test_unlinked_stereo_ways_get_their_own_side_highpass(self):
        self._commit_sub_crossover_state(
            main_highpass_enabled=True, sub_link=False,
            sub_filters={
                "left": {"family": "bessel", "slope_db_oct": 12, "frequency_hz": 60},
                "right": {"family": "butterworth", "slope_db_oct": 24, "frequency_hz": 120}})
        payload = self.fetch()
        self.assertEqual(payload["ways"]["left_low"]["derived_highpass"],
                         {"family": "bessel", "slope_db_oct": 12, "frequency_hz": 60})
        self.assertEqual(payload["ways"]["left_high"]["derived_highpass"],
                         {"family": "bessel", "slope_db_oct": 12, "frequency_hz": 60})
        self.assertEqual(payload["ways"]["right_low"]["derived_highpass"],
                         {"family": "butterworth", "slope_db_oct": 24, "frequency_hz": 120})

    def test_dual_mono_ways_share_the_crossover(self):
        # Dual-Mono has no sides: an unlinked override must stay unused.
        self._commit_sub_crossover_state(
            main_highpass_enabled=True, sub_link=False,
            assignments=["left_low", "left_high", "right_low", "right_high",
                         "sub1", "sub2"],
            sub_filters={"left": {"family": "bessel", "slope_db_oct": 12,
                                   "frequency_hz": 60}})
        payload = self.fetch()
        for role in ("left_low", "right_low"):
            with self.subTest(role=role):
                self.assertEqual(payload["ways"][role]["derived_highpass"],
                                 {"family": "linkwitz-riley", "slope_db_oct": 24,
                                  "frequency_hz": 80})

    def test_sub_highpass_off_leaves_low_way_full_range(self):
        self._commit_sub_crossover_state(main_highpass_enabled=False)
        payload = self.fetch()
        low = payload["ways"]["left_low"]
        self.assertTrue(low["complete"])
        self.assertIsNone(low["derived_highpass"])
        points = dict(low["points"])
        self.assertAlmostEqual(points[min(points)], 0.0, delta=0.2)


if __name__ == "__main__":
    unittest.main()
