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

from dsp.manager import DSPManager, UnsupportedPluginError


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

    def test_convolver_preset_is_not_rate_reload_bound(self):
        ir = self.manager.irs_dir / "room.wav"
        with wave.open(str(ir), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48000)
            handle.writeframes(b"\x00\x00\x00\x40")
        self.manager.create_convolver_preset("Room IR", ir.name)
        self.manager.load_preset("Room IR")
        texts = [self.manager.compile_engine_text(
            [{"name": "FL", "source": 0}, {"name": "FR", "source": 1}],
            sample_rate_hz=rate,
        ) for rate in (44100, 48000, 96000)]
        self.assertTrue(all('param path ' in text for text in texts))
        self.assertEqual([text.split('param path ', 1)[1].splitlines()[0] for text in texts],
                         [texts[0].split('param path ', 1)[1].splitlines()[0]] * 3)

    def test_peq_accepts_gain_and_delay_outside_lsp(self):
        self.manager.create_peq_preset("Special", {
            "enabled": True,
            "params": {"channelMode": "stereo-linked", "bands": [
                {"filterType": "gain", "frequencyHz": 1000, "gainDb": 2, "q": 1},
                {"filterType": "delay", "frequencyHz": 1000, "gainDb": 0, "q": 1,
                 "delayMs": 5},
            ]},
        })
        text = self.manager.compile_engine_text(
            [{"name": "FL", "source": 0}, {"name": "FR", "source": 1}],
            preset_name="Special")
        self.assertIn("control g_in 1.25892541", text)
        self.assertIn("native delay", text)

    def test_manager_has_no_easyeffects_runtime_import(self):
        tree = ast.parse((ROOT / "dsp/manager.py").read_text())
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertFalse(any(name.startswith("easyeffects") for name in imported))

    def test_global_helpers_keep_legacy_tone_position(self):
        extras = self.manager.normalize_effects_extras({
            "headroom": {"enabled": True}, "delay": {"enabled": True},
            "tone_effect": {"enabled": True, "mode": "crystalizer"},
            "bass_enhancer": {"enabled": True}, "autogain": {"enabled": True},
            "loudness": {"enabled": True}, "limiter": {"enabled": True},
        })
        self.assertEqual([item["type"] for item in self.manager._extras_chain(extras)], [
            "headroom", "delay", "crystalizer", "bass_enhancer",
            "autogain", "loudness", "limiter",
        ])
        extras["tone_effect"]["mode"] = "maximizer"
        self.assertEqual([item["type"] for item in self.manager._extras_chain(extras)][2], "maximizer")

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
            "limiter#0": {"bypass": False, "input-gain": -3, "output-gain": 2,
                          "threshold": -2, "attack": 5, "release": 50,
                          "lookahead": 5, "stereo-link": 100},
        }}
        result = self.manager.import_preset_json("Helpers.json", json.dumps(legacy))
        chain = json.loads(Path(result["path"]).read_text())["chain"]
        self.assertEqual([plugin["type"] for plugin in chain],
                         ["convolver", "autogain", "loudness", "limiter"])
        self.assertEqual(chain[0]["params"]["kernel"], "room")
        self.assertEqual(chain[3]["params"]["inputGainDb"], -3)
        self.assertEqual(chain[3]["params"]["outputGainDb"], 2)
        # Imported limiter time params are clamped into the engine-effective
        # plugin port range (50 ms release would silently apply as 20 ms).
        self.assertEqual(chain[3]["params"]["releaseMs"], 20)

    def test_maximizer_compilation_emits_input_gain_control(self):
        chain = [{"id": "maximizer#0", "type": "maximizer", "enabled": True,
                  "params": {"inputGainDb": 3, "thresholdDb": -0.5, "releaseMs": 30}}]
        self.manager.preset_store.write("Maximizer", self.manager._native_preset(chain))

        text = self.manager.compile_engine_text(
            [{"name": "FL", "source": 0}, {"name": "FR", "source": 1}],
            preset_name="Maximizer",
        )

        self.assertIn("stage_begin 0 maximizer#0 lv2 urn:zamaudio:ZaMaximX2", text)
        self.assertIn("control gain 3", text)

    def test_legacy_maximizer_import_preserves_input_gain(self):
        legacy = {"output": {
            "plugins_order": ["maximizer#0"],
            "maximizer#0": {"bypass": False, "input-gain": 4.5,
                            "threshold": -0.5, "release": 30},
        }}

        result = self.manager.import_preset_json("Maximizer.json", json.dumps(legacy))
        params = json.loads(Path(result["path"]).read_text())["chain"][0]["params"]

        self.assertEqual(params["inputGainDb"], 4.5)

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
        with wave.open(str(ir), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48000)
            handle.writeframes(b"\x00\x00\x00\x00")
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

    def _peq_definition(self):
        return {"enabled": True, "params": {"channelMode": "stereo-linked", "bands": [
            {"filterType": "bell", "frequencyHz": 100, "gainDb": 2, "q": 1}]}}

    def test_protected_presets_cannot_be_overwritten_by_create_import_combine(self):
        direct = (self.manager.output_dir / "Direct.json").read_text()
        neutral = (self.manager.output_dir / "Neutral.json").read_text()
        for name in ("Direct", "Neutral", "Direct.json", "Neutral.json"):
            with self.assertRaisesRegex(ValueError, "built-in preset", msg=name):
                self.manager.create_peq_preset(name, self._peq_definition())
        native = json.dumps({"schema": "fxroute.dsp.preset", "version": 1,
                             "metadata": {}, "chain": [
                                 {"id": "equalizer#0", "type": "equalizer",
                                  "params": {"channelMode": "stereo-linked", "bands": []}}]})
        with self.assertRaisesRegex(ValueError, "built-in preset"):
            self.manager.import_preset_json("Neutral.json", native)
        with self.assertRaisesRegex(ValueError, "built-in preset"):
            self.manager.import_preset_json("Direct.json", native)
        self.manager.create_peq_preset("A", self._peq_definition())
        self.manager.create_peq_preset("B", self._peq_definition())
        with self.assertRaisesRegex(ValueError, "built-in preset"):
            self.manager.combine_presets("Neutral", ["A", "B"])
        # Nothing was written over the built-ins.
        self.assertEqual((self.manager.output_dir / "Direct.json").read_text(), direct)
        self.assertEqual((self.manager.output_dir / "Neutral.json").read_text(), neutral)

    def test_protected_preset_upload_leaves_no_ir_behind(self):
        ir = self.home / "room.wav"
        with wave.open(str(ir), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48000)
            handle.writeframes(b"\x00\x00\x00\x00")
        with self.assertRaisesRegex(ValueError, "built-in preset"):
            self.manager.create_convolver_preset_with_upload(
                "Neutral", ir, ir.name)
        self.assertEqual([path.name for path in self.manager.irs_dir.iterdir()], [])
        with self.assertRaisesRegex(ValueError, "built-in preset"):
            self.manager.create_convolver_preset_with_dual_uploads(
                "Direct", ir, ir.name, ir, ir.name)
        self.assertEqual([path.name for path in self.manager.irs_dir.iterdir()], [])

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

    def _write_mono_wav(self, path, frames=b"\x00\x00\x00\x00"):
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48000)
            handle.writeframes(frames)

    def test_convolver_dual_upload_with_decimal_gain_name_round_trips(self):
        # The measurement UI generates convolver preset names that embed the
        # auto-gain value, e.g. "... -1.5dB ...".  The dot is a plain name
        # character, not a filename extension boundary: creating a second
        # preset under such a name must succeed (it previously failed with
        # "IR file not found" because Path.stem truncated the kernel at the
        # decimal point), and the preset must then load and delete cleanly.
        name = "Conv LR Min Neutral 30-12000Hz -1.5dB 130316"
        left = self.home / "left.wav"
        right = self.home / "right.wav"
        self._write_mono_wav(left)
        self._write_mono_wav(right)
        created = self.manager.create_convolver_preset_with_dual_uploads(
            name, left, left.name, right, right.name
        )
        ir = Path(created["ir"]["path"])
        self.assertEqual(created["preset"]["kernel_name"], name)
        self.assertEqual(ir.name, f"{name}.irs")
        self.assertEqual(created["preset"]["name"], name)
        # The engine config must resolve the dotted kernel back to the IR.
        self.manager.load_preset(name)
        text = self.manager.compile_engine_text(
            [{"name": "FL", "source": 0}, {"name": "FR", "source": 1}],
            preset_name=name, sample_rate_hz=48000,
        )
        self.assertIn(f"param path {json.dumps(str(ir))}", text)
        # Deleting the preset removes the now-orphaned dotted IR.
        self.manager.delete_preset(name)
        self.assertFalse(ir.exists())

    def test_convolver_dotted_kernel_resolution_preserves_inner_dots(self):
        # Regression for kernels that contain dots without a trailing
        # extension (the exact form stored in preset plugins): resolution
        # must not truncate at the first dot via pathlib stem semantics.
        ir = self.manager.irs_dir / "Conv LR Min Harman 30-12000Hz -1.5dB 130321.irs"
        self._write_mono_wav(ir)
        kernel = "Conv LR Min Harman 30-12000Hz -1.5dB 130321"
        self.assertEqual(self.manager.preset_store.find_ir_paths(kernel), [ir])
        self.assertEqual(self.manager._resolve_kernel_path(kernel), ir)
        created = self.manager.create_convolver_preset("Dotted kernel", ir.name)
        payload = json.loads(Path(created["path"]).read_text())
        self.assertEqual(payload["chain"][0]["params"]["kernel"], kernel)
        self.assertEqual(self.manager.preset_store.kernels(payload), {kernel})
        self.manager.delete_preset("Dotted kernel")
        self.assertFalse(ir.exists())

    def test_kernel_name_strips_only_a_real_ir_suffix(self):
        from dsp.persistence import kernel_name
        self.assertEqual(kernel_name("Room.irs"), "Room")
        self.assertEqual(kernel_name("Room.WAV"), "Room")
        self.assertEqual(kernel_name("Room.impulse.wav"), "Room.impulse")
        self.assertEqual(kernel_name("Room.impulse"), "Room.impulse")
        self.assertEqual(kernel_name("Conv ... -1.5dB 130316"), "Conv ... -1.5dB 130316")
        self.assertEqual(kernel_name(""), "")


if __name__ == "__main__":
    unittest.main()
