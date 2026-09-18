#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Manual bank measurements must stage the committed v2 plan, not legacy.

A manual /api/measurements/start for a crossover/bank area used to fall
back to the legacy overview route (mode label + from_overview layout), so
the pre-sweep check refused every such measurement: native mode "stereo"
never equals the derived legacy label and the v2 layout never equals the
80 Hz overview layout.  The session route now stages the committed plan
context (layout/mode/fingerprint at the measurement rate) through an
injected factory; without it (or on None) the legacy route is unchanged.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from audio.output_state import set_crossover, set_mode_routing, set_output_processing, switch_mode
from audio.output_state_store import OutputStateStore
from audio.output_service import OutputService, OutputServiceDeps
from dsp.manager import DSPManager
from measurement import session as measurement_session


PORTS = [f"playback_AUX{i}" for i in range(4)]
OVERVIEW = {
    "selected_output": {"key": "A", "channels": 4},
    "output_mode": {"effective_output_key": "A", "effective_output_channels": 4,
                    "hardware_playback_ports": list(PORTS)},
}


def make_service(directory, manager):
    return OutputService(OutputServiceDeps(
        store=OutputStateStore(Path(directory) / "output-state.json"),
        preset_loader=manager.preset_store.read,
        resolve_ir=lambda kernel: (_ for _ in ()).throw(AssertionError(kernel)),
        measurement_active=lambda: False,
    ))


def seed_stereo_two_way(service):
    switched = service.commit(switch_mode(service.load(), "stereo"), expected_revision=0)
    cros = service.commit(set_crossover(switched, "stereo", True),
                          expected_revision=switched["revision"])
    routed = service.commit(
        set_mode_routing(cros, "stereo", "A",
                         ["left_low", "left_high", "right_low", "right_high"]),
        expected_revision=cros["revision"])
    filt = {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000}
    for role in ("left_low", "right_low"):
        routed = service.commit(
            set_output_processing(routed, "stereo", role, lowpass=filt),
            expected_revision=routed["revision"])
    for role in ("left_high", "right_high"):
        routed = service.commit(
            set_output_processing(routed, "stereo", role, highpass=filt),
            expected_revision=routed["revision"])
    return routed


class StageHelperTests(unittest.TestCase):
    def test_no_factory_keeps_legacy_route(self):
        measurement_session.configure_services(SimpleNamespace(stage_bank_v2_context=None))
        try:
            self.assertEqual(
                measurement_session._stage_bank_v2_context(
                    measurement_bank="low", measurement_rate_hz=48000), {})
        finally:
            measurement_session.configure_services(SimpleNamespace())

    def test_empty_bank_keeps_legacy_route(self):
        measurement_session.configure_services(SimpleNamespace(
            stage_bank_v2_context=mock.MagicMock(side_effect=AssertionError("unused"))))
        try:
            self.assertEqual(
                measurement_session._stage_bank_v2_context(
                    measurement_bank="", measurement_rate_hz=48000), {})
        finally:
            measurement_session.configure_services(SimpleNamespace())

    def test_factory_failure_keeps_legacy_route(self):
        def raising(**kwargs):
            raise RuntimeError("nope")

        measurement_session.configure_services(SimpleNamespace(stage_bank_v2_context=raising))
        try:
            self.assertEqual(
                measurement_session._stage_bank_v2_context(
                    measurement_bank="low", measurement_rate_hz=48000), {})
        finally:
            measurement_session.configure_services(SimpleNamespace())

    def test_partial_factory_dict_is_filtered_to_known_keys(self):
        measurement_session.configure_services(SimpleNamespace(
            stage_bank_v2_context=lambda **kwargs: {"expected_native_output_mode": "stereo",
                                                    "bogus": 1}))
        try:
            self.assertEqual(
                measurement_session._stage_bank_v2_context(
                    measurement_bank="low", measurement_rate_hz=48000),
                {"expected_native_output_mode": "stereo"})
        finally:
            measurement_session.configure_services(SimpleNamespace())


class MainFactoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = DSPManager(home=Path(directory.name) / "home")
        self.service = make_service(directory.name, self.manager)
        seed_stereo_two_way(self.service)

    def test_stages_layout_mode_and_fingerprint_at_measurement_rate(self):
        with mock.patch.object(main, "get_output_service", return_value=self.service), \
             mock.patch.object(main, "get_audio_output_overview", return_value=dict(OVERVIEW)):
            staged = main._stage_bank_v2_context(measurement_bank="low",
                                                 measurement_rate_hz=48000)
        self.assertEqual(set(staged), {"expected_native_layout",
                                       "expected_native_output_mode",
                                       "expected_plan_fingerprint"})
        self.assertEqual(staged["expected_native_output_mode"], "stereo")
        self.assertEqual(len(staged["expected_native_layout"]), 4)
        self.assertTrue(all(staged["expected_native_layout"][i]["sos"] for i in range(4)))
        expect = self.service.fingerprint(
            self.service.load(), output_key="A", channels=4, sample_rate_hz=48000)
        self.assertEqual(staged["expected_plan_fingerprint"], expect)

    def test_unrenderable_head_stages_nothing(self):
        with mock.patch.object(main, "get_output_service", return_value=self.service), \
             mock.patch.object(main, "get_audio_output_overview", return_value=dict(OVERVIEW)), \
             mock.patch.object(self.service, "compile_layout",
                               side_effect=ValueError("unresolvable IR")):
            self.assertIsNone(main._stage_bank_v2_context(measurement_bank="low",
                                                          measurement_rate_hz=48000))

    def test_empty_bank_or_bad_rate_stages_nothing(self):
        with mock.patch.object(main, "get_output_service", return_value=self.service), \
             mock.patch.object(main, "get_audio_output_overview", return_value=dict(OVERVIEW)):
            self.assertIsNone(main._stage_bank_v2_context(measurement_bank="",
                                                          measurement_rate_hz=48000))
            self.assertIsNone(main._stage_bank_v2_context(measurement_bank="low",
                                                          measurement_rate_hz=0))


if __name__ == "__main__":
    unittest.main()
