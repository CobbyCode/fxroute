#!/usr/bin/env python3
import json
import copy
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp_manager import DSPManager


class DSPManagerStateTests(unittest.TestCase):
    def test_loudness_runtime_bounds_match_lsp_lv2_port(self):
        self.assertEqual(DSPManager.LOUDNESS_PLUGIN_VOLUME_MIN_DB, -83.0)
        self.assertEqual(DSPManager.LOUDNESS_PLUGIN_VOLUME_MAX_DB, 7.0)

    def test_delay_is_bounded_to_old_contract_of_500_ms(self):
        manager = DSPManager(home=self.home)
        with self.assertRaisesRegex(ValueError, "between 0 and 500"):
            manager.normalize_effects_extras({
                "delay": {"enabled": True, "params": {"leftMs": 501, "rightMs": 0}}
            })

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_bootstrap_owns_fxroute_paths_and_builtin_presets(self):
        manager = DSPManager(home=self.home)
        self.assertEqual(manager.base_dir, self.home / ".config/fxroute/dsp")
        self.assertEqual(manager.output_dir, manager.base_dir / "presets")
        self.assertEqual(manager.irs_dir, manager.base_dir / "irs")
        self.assertEqual(manager.state_dir, manager.base_dir / "state")
        self.assertEqual([item["name"] for item in manager.list_presets()][:2], ["Direct", "Neutral"])
        self.assertEqual(manager.get_active_preset(), "Neutral")
        payload = json.loads((manager.output_dir / "Direct.json").read_text())
        self.assertEqual((payload["schema"], payload["version"], payload["chain"]),
                         ("fxroute.dsp.preset", 1, []))

    def test_active_preset_and_compare_state_are_locally_persisted(self):
        manager = DSPManager(home=self.home)
        manager.load_preset("Direct")
        self.assertEqual(DSPManager(home=self.home).get_active_preset(), "Direct")
        compare = manager.save_compare_state(
            {"presetA": "Direct", "presetB": "Neutral", "activeSide": "B"}
        )
        self.assertEqual(compare["activeSide"], "B")
        self.assertEqual(DSPManager(home=self.home).load_compare_state(), compare)

    def test_runtime_properties_are_owned_locally_and_applied(self):
        calls = []
        manager = DSPManager(home=self.home, apply_callback=calls.append)
        manager.set_active_plugin_property("loudness", 0, "outputGain", -4.5)
        self.assertEqual(manager.get_active_plugin_property("loudness", 0, "outputGain"), "-4.5")
        self.assertEqual(calls[-1]["operation"], "set_property")
        self.assertEqual(calls[-1]["property"], "outputGain")
        self.assertEqual(manager.read_loudness_runtime()["output_gain"], -4.5)

    def test_failed_apply_does_not_acknowledge_runtime_or_preset_state(self):
        def reject(_event):
            raise RuntimeError("engine rejected update")

        manager = DSPManager(home=self.home, apply_callback=reject)
        old_gain = manager.get_active_plugin_property("loudness", 0, "outputGain")
        with self.assertRaisesRegex(RuntimeError, "engine rejected"):
            manager.set_active_plugin_property("loudness", 0, "outputGain", -9)
        self.assertEqual(manager.get_active_plugin_property("loudness", 0, "outputGain"), old_gain)
        with self.assertRaisesRegex(RuntimeError, "engine rejected"):
            manager.load_preset("Direct")
        self.assertEqual(manager.get_active_preset(), "Neutral")

    def test_loudness_and_autogain_offsets_preserve_canonical_level(self):
        manager = DSPManager(home=self.home)
        for strength in (1, 4, 7, 10):
            for target in (-12, -15, -18, -23):
                extras = manager.normalize_effects_extras({
                    "autogain": {"enabled": True, "params": {"targetDb": target}},
                    "loudness": {"enabled": True, "params": {
                        "strength": strength, "volumeDb": -26.5,
                        "calibration": {"requiredAdjustmentDb": 4.25},
                    }},
                })
                payload = manager._loudness_plugin_payload(
                    extras["loudness"], extras["autogain"])
                self.assertTrue(math.isclose(
                    payload["volume"] + payload["output-gain"], -26.5, abs_tol=1e-9))

    def test_runtime_transition_receives_previous_and_persists_only_in_callback(self):
        manager = DSPManager(home=self.home)
        previous = manager.load_global_extras()
        candidate = copy.deepcopy(previous)
        candidate["loudness"]["enabled"] = True
        candidate["loudness"]["params"]["volumeDb"] = -20
        calls = []

        def transition(old, new, persist_all):
            calls.append((old, new, persist_all))
            return manager.apply_global_extras_to_all_presets(new)

        manager.runtime_transition_callback = transition
        result = manager.apply_autogain_loudness_runtime(previous, candidate)
        self.assertEqual(calls[0][0], previous)
        self.assertEqual(calls[0][1], manager.normalize_effects_extras(candidate))
        self.assertTrue(calls[0][2])
        self.assertEqual(result["extras"], manager.load_global_extras())

    def test_failed_runtime_transition_does_not_persist_candidate(self):
        manager = DSPManager(home=self.home)
        previous = manager.load_global_extras()
        candidate = copy.deepcopy(previous)
        candidate["autogain"]["enabled"] = True
        manager.runtime_transition_callback = lambda *_args: (_ for _ in ()).throw(
            RuntimeError("transition failed"))
        with self.assertRaisesRegex(RuntimeError, "transition failed"):
            manager.apply_autogain_loudness_runtime(previous, candidate)
        self.assertEqual(manager.load_global_extras(), previous)


if __name__ == "__main__":
    unittest.main()
