#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
#
# Loudness runtime transition regression contracts.
#
# Restores the still-relevant contracts of the deleted EasyEffects-era
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

from dsp_manager import DSPManager


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
                         (4, 3), (3, 2), (2, 1), (1, 2), (2, 3), (5, 6), (9, 10)):
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
        for old, new in ((1, 2), (2, 3), (4, 5), (5, 6), (9, 10), (10, 9), (3, 1)):
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
            self.manager.apply_runtime_properties_from_extras(new)
            return self.manager.apply_global_extras_to_all_presets(new)

        self.manager.runtime_transition_callback = transition
        self.manager.apply_autogain_loudness_runtime(previous, candidate)
        payload = self.manager._loudness_plugin_payload(
            self.manager.normalize_effects_extras(candidate)["loudness"],
            self.manager.normalize_effects_extras(candidate)["autogain"])
        self.assertTrue(math.isclose(
            self.manager.read_loudness_runtime()["volume"], float(payload["volume"]), abs_tol=1e-9))
        self.assertTrue(math.isclose(
            self.manager.read_loudness_runtime()["output_gain"], float(payload["output-gain"]), abs_tol=1e-9))
        self.assertFalse(self.manager.read_loudness_runtime()["bypass"])

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
            self.manager.read_autogain_runtime()["target"], float(expected["target"]), abs_tol=1e-9))
        self.assertEqual(self.manager.read_autogain_runtime()["bypass"], expected["bypass"])

    def test_rollback_contract_restores_previous_readback(self):
        # Mirrors the production guarded transition: the transition applies
        # the candidate, the engine settle fails, and the rollback path
        # (apply_previous) restores the previous extras and readback state.
        previous = self.manager.load_global_extras()
        candidate = copy.deepcopy(previous)
        candidate["loudness"]["enabled"] = True
        candidate["loudness"]["params"]["volumeDb"] = -30.0

        def transition_applies_then_fails(_old, new, _persist_all):
            self.manager.apply_runtime_properties_from_extras(new)
            raise RuntimeError("settle failed; rollback required")

        self.manager.runtime_transition_callback = transition_applies_then_fails
        with self.assertRaisesRegex(RuntimeError, "rollback required"):
            self.manager.apply_autogain_loudness_runtime(previous, candidate)
        # The guarded rollback executes apply_previous: extras + readback.
        self.manager.save_global_extras(previous)
        self.manager.apply_runtime_properties_from_extras(previous)
        previous_payload = self.manager._loudness_plugin_payload(
            self.manager.normalize_effects_extras(previous)["loudness"],
            self.manager.normalize_effects_extras(previous)["autogain"])
        self.assertTrue(math.isclose(
            self.manager.read_loudness_runtime()["volume"], float(previous_payload["volume"]), abs_tol=1e-9))
        self.assertTrue(math.isclose(
            self.manager.read_loudness_runtime()["output_gain"], float(previous_payload["output-gain"]), abs_tol=1e-9))

    def test_runtime_properties_survive_manager_reload(self):
        previous = self.manager.load_global_extras()
        candidate = copy.deepcopy(previous)
        candidate["loudness"]["enabled"] = True
        candidate["loudness"]["params"]["volumeDb"] = -15.0

        def transition(old, new, persist_all):
            self.manager.apply_runtime_properties_from_extras(new)
            return self.manager.apply_global_extras_to_all_presets(new)

        self.manager.runtime_transition_callback = transition
        self.manager.apply_autogain_loudness_runtime(previous, candidate)
        reloaded = DSPManager(home=self.home)
        payload = self.manager._loudness_plugin_payload(
            self.manager.normalize_effects_extras(candidate)["loudness"],
            self.manager.normalize_effects_extras(candidate)["autogain"])
        self.assertTrue(math.isclose(
            reloaded.read_loudness_runtime()["volume"], float(payload["volume"]), abs_tol=1e-9))


if __name__ == "__main__":
    unittest.main()
