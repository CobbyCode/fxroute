#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp_manager import DSPManager


class DSPManagerStateTests(unittest.TestCase):
    def test_loudness_runtime_bounds_match_native_validation(self):
        self.assertEqual(DSPManager.LOUDNESS_PLUGIN_VOLUME_MIN_DB, -80.0)
        self.assertEqual(DSPManager.LOUDNESS_PLUGIN_VOLUME_MAX_DB, 0.0)

    def test_delay_is_bounded_to_one_second(self):
        manager = DSPManager(home=self.home)
        with self.assertRaisesRegex(ValueError, "between 0 and 1000"):
            manager.normalize_effects_extras({
                "delay": {"enabled": True, "params": {"leftMs": 1001, "rightMs": 0}}
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


if __name__ == "__main__":
    unittest.main()
