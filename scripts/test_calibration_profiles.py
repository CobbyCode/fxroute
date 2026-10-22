#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""The SPL calibration of the Loudness work point belongs to the output it was measured on."""

import asyncio
import copy
import hashlib
import json
import random
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import output_render_test_support as support
from audio.output_state import set_mode_routing, switch_mode
from dsp import calibration_profiles as profiles
from dsp.manager import DSPManager
from dsp.output_render import build_plan_target

USB = "alsa_output.usb-Focusrite_Scarlett_18i16-00"
PORTS = [f"playback_AUX{i}" for i in range(4)]


def record(identity, adjustment, created="2026-10-01T10:00:00+00:00", **extra):
    return {"outputProfileId": identity, "outputProfileLabel": f"Label {identity}", "measuredSplDb": 83.0 - adjustment,
            "requiredAdjustmentDb": adjustment, "createdAt": created, **extra}


def loudness_extras(*, calibration=None, mapping=None, legacy=None):
    params = {"fftSize": 4096, "strength": 10, "volumeDb": -20.0, "calibration": calibration or {},
              "calibrationProfiles": mapping or {}}
    if legacy:
        params["legacyCalibration"] = legacy
    return {"loudness": {"enabled": True, "params": params}}


def volume_of(text):
    return float(re.search(r"^control volume (\S+)$", text, re.MULTILINE).group(1))


class ProfileIdentityTest(unittest.TestCase):
    def test_usb_profile_switches_and_ucm_siblings_share_one_identity(self):
        self.assertEqual(profiles.profile_id(f"{USB}.multichannel-output"), USB)
        self.assertEqual(profiles.profile_id(f"{USB}.pro-output-0"), USB)
        self.assertEqual(profiles.profile_id("alsa_output.usb-Vendor_Dev-00.HiFi__Line1__sink"), "alsa_output.usb-Vendor_Dev-00")
        for plain in ("alsa_output.pci-0000_00_1f.3.analog-stereo", "alsa_output.pci-0000_00_03.0.hdmi-stereo", "out-1"):
            self.assertEqual(profiles.profile_id(plain), plain)
        for none in ("", "   ", None, 7):
            self.assertEqual(profiles.profile_id(none), "")

    def test_keys_that_share_an_identity_collapse_to_the_newest_record(self):
        mapping = {f"{USB}.pro-output-0": record("x", 2.0, created="2026-09-01T00:00:00+00:00"),
                   f"{USB}.multichannel-output": record("y", 5.0, created="2026-10-05T00:00:00+00:00"),
                   USB: record(USB, 3.0, created="2026-08-01T00:00:00+00:00")}
        migrated = profiles.migrate(loudness_extras(mapping=mapping)["loudness"]["params"])
        self.assertEqual(list(migrated), [USB])
        self.assertEqual(migrated[USB]["requiredAdjustmentDb"], 5.0)
        self.assertEqual(migrated[USB]["outputProfileId"], USB)

    def test_a_record_without_a_creation_time_loses_to_a_dated_one_and_the_last_wins_a_tie(self):
        mapping = {f"{USB}.pro-output-0": record("a", 1.0, created=""), f"{USB}.multichannel-output": record("b", 2.0)}
        self.assertEqual(profiles.migrate({"calibrationProfiles": mapping})[USB]["requiredAdjustmentDb"], 2.0)
        tie = {f"{USB}.pro-output-0": record("a", 1.0), f"{USB}.multichannel-output": record("b", 2.0)}
        self.assertEqual(profiles.migrate({"calibrationProfiles": tie})[USB]["requiredAdjustmentDb"], 2.0)

    def test_the_stored_adjustment_decides_the_calibrated_flag(self):
        migrated = profiles.migrate({"calibrationProfiles": {"a": record("a", 0.4), "b": record("b", -7.0)}})
        self.assertEqual((migrated["a"]["calibrated"], migrated["b"]["calibrated"]), (True, False))


class MigrationTest(unittest.TestCase):
    def test_a_calibration_naming_an_output_the_map_lacks_is_added_to_it(self):
        params = loudness_extras(calibration=record(f"{USB}.pro-output-0", 4.0))["loudness"]["params"]
        migrated = profiles.migrate(params)
        self.assertEqual(list(migrated), [USB])
        self.assertEqual(migrated[USB]["requiredAdjustmentDb"], 4.0)

    def test_the_map_is_the_source_of_truth_when_the_view_disagrees(self):
        params = loudness_extras(calibration=record("dev", 9.0), mapping={"dev": record("dev", 4.0)})["loudness"]["params"]
        self.assertEqual(profiles.migrate(params)["dev"]["requiredAdjustmentDb"], 4.0)

    def test_binding_twice_changes_nothing(self):
        extras = loudness_extras(calibration=record("dev", 4.0), mapping={"other": record("other", 1.0)})
        once = profiles.bind_calibration(extras, "dev")
        self.assertEqual(profiles.bind_calibration(once, "dev"), once)
        self.assertEqual(sorted(once["loudness"]["params"]["calibrationProfiles"]), ["dev", "other"])

    def test_an_unbound_calibration_is_kept_as_legacy_and_never_used(self):
        old = {"requiredAdjustmentDb": 6.0, "measuredSplDb": 77.0}
        extras = loudness_extras(calibration=old)
        for device in ("dev", None, ""):
            with self.subTest(device=device):
                bound = profiles.bind_calibration(extras, device)
                params = bound["loudness"]["params"]
                self.assertEqual(params["calibration"], {})
                self.assertEqual(params["legacyCalibration"]["requiredAdjustmentDb"], 6.0)
                self.assertEqual(params["calibrationProfiles"], {})
        again = profiles.bind_calibration(profiles.bind_calibration(extras, "dev"), "dev")
        self.assertEqual(again["loudness"]["params"]["legacyCalibration"]["requiredAdjustmentDb"], 6.0)

    def test_the_first_legacy_calibration_is_not_replaced_by_a_later_one(self):
        extras = loudness_extras(calibration={"requiredAdjustmentDb": 2.0}, legacy={"requiredAdjustmentDb": 6.0})
        self.assertEqual(profiles.bind_calibration(extras, "dev")["loudness"]["params"]["legacyCalibration"]["requiredAdjustmentDb"], 6.0)

    def test_binding_never_touches_its_input(self):
        extras = loudness_extras(calibration={"requiredAdjustmentDb": 6.0}, mapping={"dev": record("dev", 1.0)})
        before = copy.deepcopy(extras)
        profiles.bind_calibration(extras, "dev")
        self.assertEqual(extras, before)

    def test_extras_without_loudness_params_pass_through(self):
        for extras in ({}, {"loudness": {}}, {"loudness": {"params": []}}):
            self.assertEqual(profiles.bind_calibration(extras, "dev"), extras)


class EffectiveTest(unittest.TestCase):
    def test_an_unknown_output_reads_none_and_a_known_one_its_own_record(self):
        params = loudness_extras(mapping={"a": record("a", 4.0), "b": record("b", -6.0)})["loudness"]["params"]
        self.assertEqual(profiles.effective(params, "a")["requiredAdjustmentDb"], 4.0)
        self.assertEqual(profiles.effective(params, "b")["requiredAdjustmentDb"], -6.0)
        for unknown in ("c", "", None):
            self.assertEqual(profiles.effective(params, unknown), {})

    def test_an_output_never_reads_another_outputs_record(self):
        rng = random.Random(7)
        names = ["a", "b", "c", "d", f"{USB}.pro-output-0", "alsa_output.pci-x.analog-stereo"]
        for _ in range(200):
            mapping = {rng.choice(names): record("ignored", rng.uniform(-20, 20), created=f"2026-10-{rng.randint(1, 28):02d}")
                       for _ in range(rng.randint(0, 5))}
            calibration = record(rng.choice(names), rng.uniform(-20, 20)) if rng.random() < 0.5 else {}
            extras = loudness_extras(calibration=calibration, mapping=mapping)
            owners = {profiles.profile_id(key) for key in mapping} | {profiles.profile_id(calibration.get("outputProfileId"))}
            for device in names + ["other"]:
                bound = profiles.bind_calibration(extras, device)["loudness"]["params"]["calibration"]
                if bound:
                    self.assertEqual(bound["outputProfileId"], profiles.profile_id(device))
                else:
                    self.assertNotIn(profiles.profile_id(device), owners)


class ManagerBindingTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.manager = DSPManager(home=self.home)

    def store(self, **kwargs):
        self.manager.save_global_extras(loudness_extras(**kwargs))

    def adjustment(self, device_id=None):
        extras = self.manager.load_global_extras() if device_id is None else self.manager.load_global_extras(device_id=device_id)
        return extras["loudness"]["params"]["calibration"].get("requiredAdjustmentDb")

    def test_the_default_identity_is_the_output_of_the_applied_plan(self):
        self.store(mapping={"A": record("A", 4.0), "B": record("B", -6.0)})
        self.assertIsNone(self.adjustment())
        self.manager.active_device_reader = lambda: "A"
        self.assertEqual(self.adjustment(), 4.0)
        self.manager.active_device_reader = lambda: "B"
        self.assertEqual(self.adjustment(), -6.0)
        self.assertEqual(self.adjustment("A"), 4.0)
        self.assertIsNone(self.adjustment("C"))
        self.assertEqual(self.manager.get_active_device(), "B")

    def test_the_work_point_follows_the_output(self):
        self.store(mapping={"A": record("A", 4.0), "B": record("B", -6.0)})
        volumes = {}
        for device in ("A", "B", "C"):
            extras = self.manager.load_global_extras(device_id=device)
            volumes[device] = self.manager._loudness_plugin_payload(extras["loudness"], extras["autogain"])["volume"]
        self.assertEqual(len(set(volumes.values())), 3)
        uncalibrated = self.manager.load_global_extras(device_id="")
        reference = self.manager._loudness_plugin_payload(uncalibrated["loudness"], uncalibrated["autogain"])["volume"]
        self.assertEqual(volumes["C"], reference)
        self.assertAlmostEqual(volumes["A"], reference - 4.0)
        self.assertAlmostEqual(volumes["B"], reference + 6.0)

    def test_reading_the_extras_never_writes_them(self):
        self.store(calibration={"requiredAdjustmentDb": 6.0}, mapping={f"{USB}.pro-output-0": record("x", 1.0)})
        path = self.manager.global_extras_file
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        for device in (None, "A", USB):
            self.manager.load_global_extras(**({} if device is None else {"device_id": device}))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_saving_keeps_every_output_and_the_legacy_value_and_stays_readable_json(self):
        self.store(calibration={"requiredAdjustmentDb": 6.0}, mapping={"A": record("A", 4.0), "B": record("B", -6.0)})
        bound = self.manager.load_global_extras(device_id="A")
        self.manager.save_global_extras(bound)
        stored = json.loads(self.manager.global_extras_file.read_text())["loudness"]["params"]
        self.assertEqual(stored["calibration"]["requiredAdjustmentDb"], 4.0)
        self.assertEqual(sorted(stored["calibrationProfiles"]), ["A", "B"])
        self.assertEqual(stored["legacyCalibration"]["requiredAdjustmentDb"], 6.0)
        self.assertEqual(self.manager.load_global_extras(device_id="B")["loudness"]["params"]["calibration"]["requiredAdjustmentDb"], -6.0)
        self.assertEqual(self.manager.load_global_extras(device_id="C")["loudness"]["params"]["calibration"], {})

    def test_a_manager_without_an_output_reads_no_calibration(self):
        self.store(calibration=record("A", 4.0), mapping={"A": record("A", 4.0)})
        self.assertEqual(self.manager.load_global_extras()["loudness"]["params"]["calibration"], {})


class PlanBindingTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.manager = support.make_manager(directory.name)
        self.service = support.make_service(directory.name, self.manager)
        state = self.service.load()
        for device in ("A", "B", "C"):
            state = set_mode_routing(state, "stereo-sub", device, ["main_l", "main_r", "sub1", "sub1"])
        self.state = self.service.commit(switch_mode(state, "stereo-sub"), expected_revision=0)
        extras = loudness_extras(mapping={"A": record("A", 4.0), "B": record("B", -6.0)})
        self.manager.save_global_extras({**self.manager.load_global_extras(), **extras})
        self.manager.active_device_reader = lambda: "A"

    def plan(self, device, **kwargs):
        return self.service.compile_plan(self.state, output_key=device, channels=4, sample_rate_hz=48000, **kwargs)

    def calibration(self, plan):
        return plan["global"]["extras"]["loudness"]["params"]["calibration"].get("requiredAdjustmentDb")

    def text(self, plan, device):
        return build_plan_target(self.service, self.manager, plan, output_key=device, rate=48000,
                                 hardware_ports=PORTS, fingerprint=self.service.fingerprint_plan(plan)).text

    def test_a_plan_carries_the_calibration_of_the_output_it_renders_to(self):
        self.assertEqual([self.calibration(self.plan(device)) for device in ("A", "B", "C")], [4.0, -6.0, None])

    def test_a_plan_ignores_which_output_the_extras_were_bound_to(self):
        bound_to_a = self.manager.load_global_extras(device_id="A")
        self.assertEqual(self.calibration(self.plan("B", extras_override=bound_to_a)), -6.0)
        self.assertIsNone(self.calibration(self.plan("C", extras_override=bound_to_a)))

    def test_the_fingerprint_and_the_rendered_work_point_follow_the_output(self):
        plans = {device: self.plan(device) for device in ("A", "B", "C")}
        fingerprints = {device: self.service.fingerprint_plan(plan) for device, plan in plans.items()}
        self.assertEqual(len(set(fingerprints.values())), 3)
        self.assertEqual(fingerprints["A"], self.service.fingerprint_plan(self.plan("A")))
        volumes = {device: volume_of(self.text(plan, device)) for device, plan in plans.items()}
        self.assertEqual(len(set(volumes.values())), 3)
        self.assertAlmostEqual(volumes["A"] - volumes["C"], -4.0)
        self.assertAlmostEqual(volumes["B"] - volumes["C"], 6.0)

    def test_two_outputs_without_a_calibration_share_a_fingerprint(self):
        self.assertEqual(self.service.fingerprint_plan(self.plan("C")), self.service.fingerprint_plan(self.plan("C")))

    def test_the_output_key_of_a_usb_profile_switch_keeps_its_calibration(self):
        state = set_mode_routing(self.service.load(), "stereo-sub", f"{USB}.pro-output-0", ["main_l", "main_r", "sub1", "sub1"])
        self.service.commit(state, expected_revision=self.service.load()["revision"])
        extras = self.manager.load_global_extras()
        extras["loudness"]["params"]["calibrationProfiles"][USB] = record(USB, 3.0)
        self.manager.save_global_extras(extras)
        for suffix in (".pro-output-0", ".multichannel-output"):
            plan = self.service.compile_plan(self.service.load(), output_key=f"{USB}{suffix}", channels=4, sample_rate_hz=48000)
            self.assertEqual(self.calibration(plan), 3.0)

    def test_the_bypass_plan_carries_no_extras_at_all(self):
        from audio.output_state import set_bank_preset
        state = self.service.commit(set_bank_preset(self.service.load(), "stereo-sub", "global", preset="Direct"),
                                    expected_revision=self.service.load()["revision"])
        plan = self.service.compile_plan(state, output_key="A", channels=4, sample_rate_hz=48000)
        self.assertEqual(plan["global"]["extras"], {})

    def test_the_applied_checkpoint_names_the_active_output(self):
        self.assertIsNone(self.service.applied_device_key())
        self.service.remember_applied(self.state, output_key=f"{USB}.pro-output-0", channels=4,
                                      hardware_ports=PORTS, sample_rate_hz=48000)
        self.assertEqual(self.service.applied_device_key(), USB)
        self.service.remember_applied(self.state, output_key="B", channels=4, hardware_ports=PORTS, sample_rate_hz=48000)
        self.assertEqual(self.service.applied_device_key(), "B")
        self.manager.active_device_reader = self.service.applied_device_key
        self.assertEqual(self.manager.load_global_extras()["loudness"]["params"]["calibration"]["requiredAdjustmentDb"], -6.0)
        self.service._applied_path().write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.service.applied_device_key())


class RendererBindingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = support.make_manager(directory.name)
        self.service = support.make_service(directory.name, self.manager)
        state = set_mode_routing(self.service.load(), "stereo-sub", "A", ["main_l", "main_r", "sub1", "sub1"])
        self.service.commit(switch_mode(state, "stereo-sub"), expected_revision=0)
        extras = loudness_extras(mapping={"A": record("A", 4.0), "B": record("B", -6.0)})
        self.manager.save_global_extras({**self.manager.load_global_extras(), **extras})
        engine = mock.MagicMock()
        engine.snapshot.return_value = {"active": False, "config": {}}
        engine.fail_closed = mock.AsyncMock()
        engine.preflight_rendered = mock.AsyncMock()
        self.harness = support.RenderHarness(self.service, self.manager, engine)

    async def test_the_rendered_graph_carries_the_calibration_of_the_output_being_rendered(self):
        for applied in ("B", None, "A"):
            with self.subTest(applied=applied):
                self.manager.active_device_reader = lambda: applied
                target = await self.harness.renderer.render_sync_target(48000, self.harness.overview)
                self.assertTrue(re.search(r"^control volume", target.text, re.MULTILINE))
                self.assertEqual(self.calibration_of(target), 4.0)

    def calibration_of(self, target):
        plan_volume = volume_of(target.text)
        extras = self.manager.load_global_extras(device_id="A")
        payload = self.manager._loudness_plugin_payload(extras["loudness"], extras["autogain"])
        return 4.0 if abs(plan_volume - payload["volume"]) < 1e-6 else None

    async def test_the_extras_check_does_not_flip_when_the_applied_output_changes(self):
        self.manager.active_device_reader = lambda: "B"
        target = await self.harness.renderer.render_sync_target(48000, self.harness.overview)
        self.manager.active_device_reader = lambda: "A"
        target.validate_current()
        self.manager.active_device_reader = lambda: None
        target.validate_current()


if __name__ == "__main__":
    unittest.main()
