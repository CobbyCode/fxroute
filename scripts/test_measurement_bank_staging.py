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
# A device wide enough for the six active roles of a stereo-sub 2-way
# crossover with two subs. The topology truncates assignments to the device
# channel capacity, so a stereo-sub expectation of six outputs is only
# reachable on such a device (the .104 interface reports 18).
OVERVIEW_SIX = {
    "selected_output": {"key": "A", "channels": 6},
    "output_mode": {"effective_output_key": "A", "effective_output_channels": 6,
                    "hardware_playback_ports": [f"playback_AUX{i}" for i in range(6)]},
}
OVERVIEW_FIVE = {
    "selected_output": {"key": "A", "channels": 5},
    "output_mode": {"effective_output_key": "A", "effective_output_channels": 5,
                    "hardware_playback_ports": [f"playback_AUX{i}" for i in range(5)]},
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


def seed_stereo_sub_two_way(service):
    """A stereo-sub 2-way crossover with two independent subs (six outputs)."""
    switched = service.commit(switch_mode(service.load(), "stereo-sub"), expected_revision=0)
    cros = service.commit(set_crossover(switched, "stereo-sub", True),
                          expected_revision=switched["revision"])
    routed = service.commit(
        set_mode_routing(cros, "stereo-sub", "A",
                         ["left_low", "left_high", "right_low", "right_high", "sub1", "sub2"]),
        expected_revision=cros["revision"])
    filt = {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000}
    for role in ("left_low", "right_low"):
        routed = service.commit(
            set_output_processing(routed, "stereo-sub", role, lowpass=filt),
            expected_revision=routed["revision"])
    for role in ("left_high", "right_high"):
        routed = service.commit(
            set_output_processing(routed, "stereo-sub", role, highpass=filt),
            expected_revision=routed["revision"])
    return routed


def seed_stereo_sub_one_sub(service):
    """A stereo-sub 2-way crossover with a single sub (five outputs)."""
    switched = service.commit(switch_mode(service.load(), "stereo-sub"), expected_revision=0)
    cros = service.commit(set_crossover(switched, "stereo-sub", True),
                          expected_revision=switched["revision"])
    routed = service.commit(
        set_mode_routing(cros, "stereo-sub", "A",
                         ["left_low", "left_high", "right_low", "right_high", "sub1"]),
        expected_revision=cros["revision"])
    filt = {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 2000}
    for role in ("left_low", "right_low"):
        routed = service.commit(
            set_output_processing(routed, "stereo-sub", role, lowpass=filt),
            expected_revision=routed["revision"])
    for role in ("left_high", "right_high"):
        routed = service.commit(
            set_output_processing(routed, "stereo-sub", role, highpass=filt),
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

    def test_empty_bank_is_staged_from_the_effective_selection(self):
        # A manual sweep without a named area bank used to keep the legacy
        # route, which describes four outputs and the legacy mode label. The
        # effective editing selection is resolvable, so the committed v2 plan
        # must be staged instead.
        measurement_session.configure_services(SimpleNamespace(
            stage_bank_v2_context=lambda **kwargs: {"expected_native_output_mode": "stereo-sub"}))
        try:
            self.assertEqual(
                measurement_session._stage_bank_v2_context(
                    measurement_bank="", measurement_rate_hz=48000),
                {"expected_native_output_mode": "stereo-sub"})
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
            self.service.load(), output_key="A", channels=6, sample_rate_hz=48000)
        self.assertEqual(staged["expected_plan_fingerprint"], expect)

    def test_unrenderable_head_stages_nothing(self):
        with mock.patch.object(main, "get_output_service", return_value=self.service), \
             mock.patch.object(main, "get_audio_output_overview", return_value=dict(OVERVIEW)), \
             mock.patch.object(self.service, "compile_layout",
                               side_effect=ValueError("unresolvable IR")):
            self.assertIsNone(main._stage_bank_v2_context(measurement_bank="low",
                                                          measurement_rate_hz=48000))

    def test_bad_rate_stages_nothing(self):
        # An unusable rate still stages nothing: without a rate the plan (and
        # therefore the expected layout and fingerprint) cannot be compiled.
        with mock.patch.object(main, "get_output_service", return_value=self.service), \
             mock.patch.object(main, "get_audio_output_overview", return_value=dict(OVERVIEW)):
            self.assertIsNone(main._stage_bank_v2_context(measurement_bank="low",
                                                          measurement_rate_hz=0))
            self.assertIsNone(main._stage_bank_v2_context(measurement_bank="",
                                                          measurement_rate_hz=0))


class StereoSubCrossoverStagingTests(unittest.TestCase):
    """A stereo-sub 2-way crossover must stage the v2 plan, not a 4-output guess.

    The legacy overview translation reports the mode as "subwoofer-2.2" and
    always describes four outputs, while the engine runs the v2
    "stereo-sub" crossover with one layout entry per active role. A manual
    sweep without a named bank used to take that legacy route, so the
    pre-sweep check refused it with a mode mismatch and "does not expose 4
    outputs" even though the routing was complete and consistent.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = DSPManager(home=Path(directory.name) / "home")
        self.service = make_service(directory.name, self.manager)

    def _staged(self, overview, **kwargs):
        kwargs.setdefault("measurement_rate_hz", 48000)
        with mock.patch.object(main, "get_output_service", return_value=self.service), \
             mock.patch.object(main, "get_audio_output_overview", return_value=dict(overview)):
            return main._stage_bank_v2_context(**kwargs)

    def test_left_channel_without_bank_stages_the_six_way_plan(self):
        seed_stereo_sub_two_way(self.service)
        staged = self._staged(OVERVIEW_SIX, measurement_bank="", channel="left")
        self.assertIsNotNone(staged)
        self.assertEqual(staged["expected_native_output_mode"], "stereo-sub")
        self.assertEqual([entry["name"] for entry in staged["expected_native_layout"]],
                         ["left_low", "left_high", "right_low", "right_high", "sub1", "sub2"])
        expect = self.service.fingerprint(
            self.service.load(), output_key="A", channels=6, sample_rate_hz=48000)
        self.assertEqual(staged["expected_plan_fingerprint"], expect)

    def test_right_channel_without_bank_stages_the_same_plan(self):
        seed_stereo_sub_two_way(self.service)
        staged = self._staged(OVERVIEW_SIX, measurement_bank="", channel="right")
        self.assertIsNotNone(staged)
        self.assertEqual(staged["expected_native_output_mode"], "stereo-sub")
        self.assertEqual(len(staged["expected_native_layout"]), 6)

    def test_named_area_bank_still_stages_the_same_plan(self):
        seed_stereo_sub_two_way(self.service)
        for bank in ("low", "high", "sub1", "sub2", "global"):
            with self.subTest(bank=bank):
                staged = self._staged(OVERVIEW_SIX, measurement_bank=bank, channel="left")
                self.assertIsNotNone(staged)
                self.assertEqual(staged["expected_native_output_mode"], "stereo-sub")
                self.assertEqual(len(staged["expected_native_layout"]), 6)

    def test_global_stereo_area_stays_green(self):
        # The previously working path: a named Global bank in stereo mode.
        seed_stereo_sub_two_way(self.service)
        staged = self._staged(OVERVIEW_SIX, measurement_bank="global", channel="stereo")
        self.assertIsNotNone(staged)
        self.assertEqual(staged["expected_native_output_mode"], "stereo-sub")
        self.assertEqual(len(staged["expected_native_layout"]), 6)

    def test_single_sub_configuration_keeps_its_own_output_count(self):
        # A one-sub setup is five outputs, not six and not the legacy four.
        seed_stereo_sub_one_sub(self.service)
        staged = self._staged(OVERVIEW_FIVE, measurement_bank="", channel="left")
        self.assertIsNotNone(staged)
        self.assertEqual(staged["expected_native_output_mode"], "stereo-sub")
        self.assertEqual(len(staged["expected_native_layout"]), 5)
        self.assertIn("sub1", [entry["name"] for entry in staged["expected_native_layout"]])
        self.assertNotIn("sub2", [entry["name"] for entry in staged["expected_native_layout"]])

    def test_stereo_two_way_without_subs_keeps_four_outputs(self):
        # The pre-existing 2.1/2.2-shaped configuration stays valid at four.
        seed_stereo_two_way(self.service)
        staged = self._staged(OVERVIEW, measurement_bank="low", measurement_rate_hz=48000)
        self.assertIsNotNone(staged)
        self.assertEqual(staged["expected_native_output_mode"], "stereo")
        self.assertEqual(len(staged["expected_native_layout"]), 4)

    def test_unactivatable_routing_stages_nothing(self):
        # A genuinely incomplete routing must still fail closed rather than
        # guess an output count: crossover on with no ways routed cannot
        # compile a plan.
        switched = self.service.commit(switch_mode(self.service.load(), "stereo-sub"),
                                       expected_revision=0)
        cros = self.service.commit(set_crossover(switched, "stereo-sub", True),
                                   expected_revision=switched["revision"])
        self.service.commit(set_mode_routing(cros, "stereo-sub", "A", ["off", "off", "off", "off"]),
                            expected_revision=cros["revision"])
        self.assertIsNone(self._staged(OVERVIEW_SIX, measurement_bank="", channel="left"))

    def test_bad_rate_stages_nothing(self):
        seed_stereo_sub_two_way(self.service)
        with mock.patch.object(main, "get_output_service", return_value=self.service), \
             mock.patch.object(main, "get_audio_output_overview", return_value=dict(OVERVIEW)):
            self.assertIsNone(main._stage_bank_v2_context(measurement_bank="",
                                                          measurement_rate_hz=0))
            self.assertIsNone(main._stage_bank_v2_context(measurement_bank="low",
                                                          measurement_rate_hz=0))


class SessionFactoryWithoutBankTests(unittest.TestCase):
    """The session helper must reach the factory for a manual sweep with no bank."""

    def tearDown(self):
        measurement_session.configure_services(SimpleNamespace())

    def test_empty_bank_is_staged_from_the_effective_selection(self):
        seen = {}

        def factory(**kwargs):
            seen.update(kwargs)
            return {"expected_native_output_mode": "stereo-sub",
                    "expected_native_layout": [{"name": "left_low"}],
                    "expected_plan_fingerprint": "fp"}

        measurement_session.configure_services(SimpleNamespace(stage_bank_v2_context=factory))
        staged = measurement_session._stage_bank_v2_context(
            measurement_bank="", measurement_rate_hz=48000)
        self.assertEqual(staged["expected_native_output_mode"], "stereo-sub")
        self.assertEqual(seen, {"measurement_bank": "", "measurement_rate_hz": 48000,
                                "channel": ""})

    def test_requested_channel_reaches_the_factory(self):
        seen = {}

        def factory(**kwargs):
            seen.update(kwargs)
            return {"expected_native_output_mode": "stereo-sub"}

        measurement_session.configure_services(SimpleNamespace(stage_bank_v2_context=factory))
        measurement_session._stage_bank_v2_context(
            measurement_bank="", measurement_rate_hz=48000, channel="right")
        self.assertEqual(seen.get("channel"), "right")

    def test_absent_factory_keeps_legacy_route(self):
        measurement_session.configure_services(SimpleNamespace(stage_bank_v2_context=None))
        self.assertEqual(measurement_session._stage_bank_v2_context(
            measurement_bank="", measurement_rate_hz=48000), {})


if __name__ == "__main__":
    unittest.main()
