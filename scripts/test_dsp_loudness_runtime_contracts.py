#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
#
# Loudness runtime transition regression contracts.
#
# Restores the still-relevant contracts of the deleted pre-native-DSP
# loudness suites (adjacent strength changes, no positive level jumps,
# safe failure/rollback) against the native DSP manager, plus the guarded
# transition readback contract: after a successful transition the runtime
# readback reports the confirmed state, and rollback restores it.

import copy
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp.manager import DSPManager
from dsp.runtime import DSPRuntime


class FakeProcess:
    returncode = None
    pid = 4242

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def extras(strength, *, volume_db=-25.212984202991393, calibration_db=17.6):
    return {
        "loudness": {
            "enabled": True,
            "params": {
                "fftSize": 8192,
                "strength": strength,
                "volumeDb": volume_db,
                "calibration": {"requiredAdjustmentDb": calibration_db},
                "calibrationProfiles": {},
            },
        },
    }


class LoudnessRuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="fxroute-loudness-contract-"))
        self.manager = DSPManager(home=self.home)
        self.runtime = DSPRuntime(self.manager, binary="fxroute-dsp")
        self.runtime._process = FakeProcess()

    def compiled(self, extras_payload):
        return self.manager.compile_engine_text(
            [{"name": "left", "source": 0}, {"name": "right", "source": 1}],
            preset_name="Neutral", extras_override=extras_payload)

    def running_readback(self, extras=None):
        return self.runtime.read_loudness_runtime(extras)

    def payload_level_db(self, extras_payload):
        normalized = self.manager.normalize_effects_extras(extras_payload)
        payload = self.manager._loudness_plugin_payload(
            normalized["loudness"], normalized["autogain"])
        return float(payload["volume"]) + float(payload["output-gain"])

    def test_adjacent_strength_transitions_preserve_total_level(self):
        # The plugin net trim is always 0 dB (canonical volume lives in the
        # engine output gain), so no strength transition changes the level
        # carried by the stage itself.
        for old, new in ((10, 9), (9, 8), (8, 7), (7, 6), (6, 5), (5, 4),
                         (4, 3), (3, 2), (2, 1), (1, 0), (0, 1), (1, 2), (2, 3), (5, 6), (9, 10)):
            self.assertTrue(math.isclose(
                self.payload_level_db(extras(old)),
                self.payload_level_db(extras(new)),
                abs_tol=1e-9,
            ), f"level jump between strengths {old} -> {new}")
            self.assertTrue(math.isclose(
                self.payload_level_db(extras(old)), 0.0, abs_tol=1e-9))
            self.assertTrue(math.isclose(
                self.payload_level_db(extras(new)), 0.0, abs_tol=1e-9))

    def test_guard_keeps_ramp_start_below_previous_level(self):
        # The audible level during the guarded rebuild is the engine gain
        # (volume + guard) plus the stage net trim (0 dB); the guard must
        # keep the ramp start below the previous level by the guard margin.
        for old, new in ((1, 2), (2, 3), (4, 5), (5, 6), (9, 10), (10, 9), (3, 1), (1, 0), (0, 1), (10, 0), (0, 10)):
            guard = self.manager.loudness_transition_guard_db(extras(old), extras(new))
            old_payload = self.manager._loudness_plugin_payload(
                self.manager.normalize_effects_extras(extras(old))["loudness"],
                self.manager.normalize_effects_extras(extras(old))["autogain"])
            new_payload = self.manager._loudness_plugin_payload(
                self.manager.normalize_effects_extras(extras(new))["loudness"],
                self.manager.normalize_effects_extras(extras(new))["autogain"])
            expected = max(
                self.manager.LOUDNESS_OUTPUT_GAIN_MIN_DB,
                min(0.0, float(old_payload["output-gain"]), float(new_payload["output-gain"]))
                - self.manager.LOUDNESS_STRENGTH_GUARD_DB,
            )
            self.assertTrue(math.isclose(guard, expected, abs_tol=1e-9))
            volume_db = float(extras(old)["loudness"]["params"]["volumeDb"])
            old_level = volume_db + float(old_payload["volume"]) + float(old_payload["output-gain"])
            ramp_start = volume_db + guard + float(new_payload["volume"]) + float(new_payload["output-gain"])
            self.assertLessEqual(ramp_start, old_level - self.manager.LOUDNESS_STRENGTH_GUARD_DB + 1e-9,
                                 f"positive jump risk between strengths {old} -> {new}")

    def test_strength_range_is_zero_to_ten_with_legacy_names_kept(self):
        for value, expected in ((0, 0), ("0", 0), (10, 10), ("7", 7), ("min", 1), ("light", 4), ("med", 7), ("full", 10)):
            with self.subTest(value=value):
                normalized = self.manager.normalize_effects_extras(extras(value))
                self.assertEqual(normalized["loudness"]["params"]["strength"], expected)
        for value in (-1, 11, 2.5):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "between 0 and 10"):
                self.manager.normalize_effects_extras(extras(value))
        with self.assertRaises(ValueError):
            self.manager.normalize_effects_extras(extras("x"))

    def test_strength_zero_is_the_flat_work_point_whatever_the_volume(self):
        # Strength 0 means no tonal effect: the plugin sits at its 0 dB reference
        # point (flat contour) and the compensating trim is 0 dB too, for any
        # volume, calibration and AutoGain target.
        for volume_db, calibration_db in ((-25.2, 17.6), (-60.0, 0.0), (0.0, -30.0)):
            for autogain in ({}, {"autogain": {"enabled": True, "params": {"targetDb": -12.0}}}):
                with self.subTest(volume_db=volume_db, calibration_db=calibration_db, autogain=bool(autogain)):
                    payload_extras = {**extras(0, volume_db=volume_db, calibration_db=calibration_db), **autogain}
                    normalized = self.manager.normalize_effects_extras(payload_extras)
                    self.assertEqual(normalized["loudness"]["params"]["strength"], 0)
                    payload = self.manager._loudness_plugin_payload(normalized["loudness"], normalized["autogain"])
                    self.assertEqual(payload["volume"], 0.0)
                    self.assertEqual(payload["output-gain"], 0.0)
        # Strength 1 is unchanged: it still shifts the work point by 30 dB.
        weakest = self.manager.normalize_effects_extras(extras(1, volume_db=-60.0, calibration_db=0.0))
        self.assertAlmostEqual(
            self.manager._loudness_plugin_payload(weakest["loudness"], weakest["autogain"])["volume"], -30.0)

    def test_strength_zero_keeps_the_stage_in_the_chain_with_the_same_shape(self):
        text_one = self.compiled(extras(1))
        text_zero = self.compiled(extras(0))
        self.assertIn("lv2 http://lsp-plug.in/plugins/lv2/loud_comp_stereo", text_zero)
        self.assertIn("control volume 0\n", text_zero)
        self.assertIn("param output_gain_db 0\n", text_zero)
        old_shape, _ = DSPRuntime._live_config(text_one)
        new_shape, _ = DSPRuntime._live_config(text_zero)
        self.assertEqual(old_shape, new_shape)

    def test_guard_stays_within_engine_floor(self):
        guard = self.manager.loudness_transition_guard_db(
            extras(10, volume_db=-40.0, calibration_db=-50.0),
            extras(1, volume_db=-40.0, calibration_db=-50.0),
        )
        # The plugin net trim is 0 dB on both sides; the guard is the deeper
        # plugin trim minus the guard margin and stays inside the engine
        # floor.
        self.assertEqual(guard, -7.0 - self.manager.LOUDNESS_STRENGTH_GUARD_DB)
        self.assertGreaterEqual(guard, self.manager.LOUDNESS_OUTPUT_GAIN_MIN_DB)

    def test_successful_transition_readback_matches_candidate(self):
        previous = self.manager.load_global_extras()
        candidate = copy.deepcopy(previous)
        candidate["loudness"]["enabled"] = True
        candidate["loudness"]["params"]["volumeDb"] = -20.0
        candidate["loudness"]["params"]["strength"] = 7

        def transition(old, new, persist_all):
            return self.manager.apply_global_extras_to_all_presets(new)

        self.manager.runtime_transition_callback = transition
        self.manager.apply_autogain_loudness_runtime(previous, candidate)
        payload = self.manager._loudness_plugin_payload(
            self.manager.normalize_effects_extras(candidate)["loudness"],
            self.manager.normalize_effects_extras(candidate)["autogain"])
        self.assertTrue(math.isclose(
            self.running_readback()["volume"], float(payload["volume"]), abs_tol=1e-9))
        self.assertTrue(math.isclose(
            self.running_readback()["output_gain"], float(payload["output-gain"]), abs_tol=1e-9))
        self.assertFalse(self.running_readback()["bypass"])

    def test_failed_transition_leaves_readback_on_previous_state(self):
        previous = self.manager.load_global_extras()
        candidate = copy.deepcopy(previous)
        candidate["autogain"]["enabled"] = True
        candidate["autogain"]["params"]["targetDb"] = -18.0

        def failing_transition(_old, _new, _persist_all):
            raise RuntimeError("engine rejected candidate")

        self.manager.runtime_transition_callback = failing_transition
        with self.assertRaisesRegex(RuntimeError, "engine rejected candidate"):
            self.manager.apply_autogain_loudness_runtime(previous, candidate)
        expected = self.manager._autogain_plugin_payload(previous["autogain"])
        self.assertTrue(math.isclose(
            self.runtime.read_autogain_runtime()["target"], float(expected["target"]), abs_tol=1e-9))
        self.assertEqual(self.runtime.read_autogain_runtime()["bypass"], expected["bypass"])

    def test_rollback_contract_restores_previous_readback(self):
        # Mirrors the production guarded transition: the transition applies
        # the candidate, the engine settle fails, and the rollback path
        # (apply_previous) restores the previous extras, which the readback
        # derives its work point from.
        previous = self.manager.load_global_extras()
        candidate = copy.deepcopy(previous)
        candidate["loudness"]["enabled"] = True
        candidate["loudness"]["params"]["volumeDb"] = -30.0

        def transition_applies_then_fails(_old, new, _persist_all):
            self.manager.apply_global_extras_to_all_presets(new)
            raise RuntimeError("settle failed; rollback required")

        self.manager.runtime_transition_callback = transition_applies_then_fails
        with self.assertRaisesRegex(RuntimeError, "rollback required"):
            self.manager.apply_autogain_loudness_runtime(previous, candidate)
        # The guarded rollback executes apply_previous: extras restore.
        self.manager.save_global_extras(previous)
        previous_payload = self.manager._loudness_plugin_payload(
            self.manager.normalize_effects_extras(previous)["loudness"],
            self.manager.normalize_effects_extras(previous)["autogain"])
        self.assertTrue(math.isclose(
            self.running_readback()["volume"], float(previous_payload["volume"]), abs_tol=1e-9))
        self.assertTrue(math.isclose(
            self.running_readback()["output_gain"], float(previous_payload["output-gain"]), abs_tol=1e-9))

    def test_confirmed_work_point_survives_manager_reload(self):
        previous = self.manager.load_global_extras()
        candidate = copy.deepcopy(previous)
        candidate["loudness"]["enabled"] = True
        candidate["loudness"]["params"]["volumeDb"] = -15.0

        def transition(old, new, persist_all):
            return self.manager.apply_global_extras_to_all_presets(new)

        self.manager.runtime_transition_callback = transition
        self.manager.apply_autogain_loudness_runtime(previous, candidate)
        reloaded = DSPManager(home=self.home)
        payload = self.manager._loudness_plugin_payload(
            self.manager.normalize_effects_extras(candidate)["loudness"],
            self.manager.normalize_effects_extras(candidate)["autogain"])
        runtime = DSPRuntime(reloaded, binary="fxroute-dsp")
        runtime._process = FakeProcess()
        self.assertTrue(math.isclose(
            runtime.read_loudness_runtime()["volume"], float(payload["volume"]), abs_tol=1e-9))

    def test_readback_requires_a_running_engine(self):
        # The native engine is the authority on liveness: without a running
        # process there is no confirmed live state to report.
        runtime = DSPRuntime(self.manager, binary="fxroute-dsp")
        with self.assertRaisesRegex(RuntimeError, "engine is not active"):
            runtime.read_loudness_runtime()
        with self.assertRaisesRegex(RuntimeError, "engine is not active"):
            runtime.read_autogain_runtime()


if __name__ == "__main__":
    unittest.main()
