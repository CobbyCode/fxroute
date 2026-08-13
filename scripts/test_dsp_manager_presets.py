#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
import wave
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsp_manager import DSPManager, UnsupportedPluginError


class DSPManagerPresetTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.manager = DSPManager(home=self.home)

    def test_create_peq_and_compile_for_arbitrary_layout(self):
        self.manager.create_peq_preset("Room", {
            "enabled": True,
            "params": {"channelMode": "stereo-linked", "bands": [
                {"filterType": "bell", "frequencyHz": 80, "gainDb": -3, "q": 1.2}
            ]},
        })
        self.manager.load_preset("Room")
        config = self.manager.compile_engine_config(
            [{"name": "front-left", "source": 0}, {"name": "sub", "source": 0}],
            sample_rate_hz=96000,
        )
        self.assertEqual((config["schema"], config["version"]), ("fxroute.dsp.engine", 1))
        self.assertEqual([channel["name"] for channel in config["outputs"]], ["front-left", "sub"])
        self.assertEqual(config["sample_rate_hz"], 96000)
        self.assertEqual(config["chain"][0]["type"], "equalizer")

    def test_manager_has_no_easyeffects_runtime_import(self):
        tree = ast.parse((ROOT / "dsp_manager.py").read_text())
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertFalse(any(name.startswith("easyeffects") for name in imported))

    def test_engine_text_uses_sparse_routes_for_stereo_and_subwoofers(self):
        stereo = self.manager.compile_engine_text(
            [{"name": "FL", "routes": [{"input": 0, "gain": 1.0}]},
             {"name": "FR", "routes": [{"input": 1, "gain": 1.0}]}],
            sample_rate_hz=48000,
        )
        self.assertIn("inputs 2\noutputs 2\n", stereo)
        self.assertIn("matrix 0 0 1", stereo)
        self.assertIn("matrix 1 1 1", stereo)

        bass = self.manager.compile_engine_text(
            [{"name": "FL", "routes": [{"input": 0, "gain": 1.0}]},
             {"name": "FR", "routes": [{"input": 1, "gain": 1.0}]},
             {"name": "SUB1", "routes": [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}]},
             {"name": "SUB2", "routes": [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}]}],
            sample_rate_hz=96000,
        )
        self.assertIn("rate 96000", bass)
        self.assertIn("matrix 2 0 0.5", bass)
        self.assertIn("matrix 3 1 0.5", bass)

    def test_legacy_import_translates_supported_plugins_and_rejects_unknown(self):
        legacy = {"output": {
            "plugins_order": ["equalizer#0", "delay#0"],
            "equalizer#0": {"bypass": False, "input-gain": -1.0, "output-gain": 0.0,
                            "split-channels": False, "mode": "IIR", "num-bands": 0,
                            "left": {}, "right": {}},
            "delay#0": {"bypass": False, "time-l": 2.0, "time-r": 3.0},
        }}
        imported = self.manager.import_preset_json("Legacy.json", json.dumps(legacy))
        payload = json.loads(Path(imported["path"]).read_text())
        self.assertEqual([item["type"] for item in payload["chain"]], ["equalizer", "delay"])

        legacy["output"]["plugins_order"].append("compressor#0")
        legacy["output"]["compressor#0"] = {"bypass": False}
        with self.assertRaisesRegex(UnsupportedPluginError, "compressor"):
            self.manager.import_preset_json("Bad.json", json.dumps(legacy))

    def test_legacy_convolver_and_helper_plugins_are_not_dropped(self):
        legacy = {"output": {
            "plugins_order": ["convolver#0", "autogain#0", "loudness#0", "limiter#0"],
            "convolver#0": {"bypass": False, "kernel-name": "room", "wet": -1},
            "autogain#0": {"bypass": False, "target": -18},
            "loudness#0": {"bypass": False, "fft": "4096", "volume": -3,
                           "output-gain": -1},
            "limiter#0": {"bypass": False, "threshold": -2, "attack": 5,
                          "release": 50, "lookahead": 5, "stereo-link": 100},
        }}
        result = self.manager.import_preset_json("Helpers.json", json.dumps(legacy))
        chain = json.loads(Path(result["path"]).read_text())["chain"]
        self.assertEqual([plugin["type"] for plugin in chain],
                         ["convolver", "autogain", "loudness", "limiter"])
        self.assertEqual(chain[0]["params"]["kernel"], "room")

    def test_native_import_rejects_unsupported_plugin(self):
        payload = {"schema": "fxroute.dsp.preset", "version": 1, "metadata": {},
                   "chain": [{"id": "mystery#0", "type": "mystery", "params": {}}]}
        with self.assertRaisesRegex(UnsupportedPluginError, "mystery"):
            self.manager.import_preset_json("Mystery.json", json.dumps(payload))

    def test_rew_import_creates_native_equalizer(self):
        rew = "Equaliser: Generic\n1 True Auto PK 46.30 -4.80 3.387\n"
        created = self.manager.create_peq_preset_from_rew_text("REW", rew)
        payload = json.loads(Path(created["path"]).read_text())
        self.assertEqual(payload["chain"][0]["params"]["bands"][0]["frequencyHz"], 46.3)

    def test_convolver_combine_export_and_delete_orphan_ir(self):
        ir = self.manager.irs_dir / "room.irs"
        ir.write_bytes(b"ir")
        self.manager.create_convolver_preset("Conv", ir.name)
        self.manager.create_peq_preset("EQ", {"params": {"bands": [
            {"filterType": "bell", "frequencyHz": 100, "gainDb": 2, "q": 1}
        ]}})
        combined = self.manager.combine_presets("Both", ["Conv", "EQ"])
        exported = json.loads(self.manager.export_preset_json("Both"))
        self.assertEqual(exported["metadata"]["source_presets"], ["Conv", "EQ"])
        self.assertEqual(combined["plugin_count"], 2)
        self.manager.delete_preset("Conv")
        self.assertTrue(ir.exists())
        self.manager.delete_preset("Both")
        self.assertFalse(ir.exists())

    def test_dual_ir_upload_merges_mono_wav_channels(self):
        left = self.home / "left.wav"
        right = self.home / "right.wav"
        for path, samples in ((left, b"\x01\x00\x02\x00"),
                              (right, b"\x03\x00\x04\x00")):
            with wave.open(str(path), "wb") as output:
                output.setparams((1, 2, 48000, 2, "NONE", ""))
                output.writeframes(samples)
        created = self.manager.create_convolver_preset_with_dual_uploads(
            "Stereo IR", left, left.name, right, right.name
        )
        merged = Path(created["ir"]["path"])
        with wave.open(str(merged), "rb") as source:
            self.assertEqual(source.getnchannels(), 2)
            self.assertEqual(source.readframes(2), b"\x01\x00\x03\x00\x02\x00\x04\x00")
        self.assertEqual(created["preset"]["kernel_name"], "Stereo IR")


if __name__ == "__main__":
    unittest.main()
