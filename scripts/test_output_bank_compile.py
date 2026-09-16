#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Plan-to-layout compilation: crossover SOS, PEQ banks, convolver banks."""

import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.output_state import default_output_state, select_bank, set_mode_routing, switch_mode
from dsp.crossover import design_crossover
from dsp.manager import DSPManager, build_wav_bytes, ensure_kernel_supported_ir, parse_wav_frames
from dsp.native_config import layout_from_plan


def bell(freq, gain=0.0):
    return {"filterType": "bell", "frequencyHz": freq, "gainDb": gain, "q": 1.0}


def eq_preset(chain_mode="stereo-linked", bands=None, left=None, right=None, eq_mode="IIR"):
    params = {"channelMode": chain_mode, "eqMode": eq_mode}
    if chain_mode == "dual":
        params["leftBands"] = left or []
        params["rightBands"] = right or []
    else:
        params["bands"] = bands or []
    return params


def eq_plugin(params):
    return {"id": "eq", "type": "equalizer", "enabled": True, "params": params}


class BankCompileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name) / "home"
        self.manager = DSPManager(home=self.home)
        store = self.manager.preset_store
        for name, chain in (
            ("Neutral", []),
            ("Direct", []),
            ("Mid EQ", [eq_plugin(eq_preset(bands=[bell(1000, -3)]))]),
            ("GainDelay EQ", [eq_plugin(eq_preset(bands=[
                bell(2000, 1), {"filterType": "gain", "frequencyHz": 1000, "gainDb": 2, "q": 1},
                {"filterType": "delay", "frequencyHz": 1000, "gainDb": 0, "q": 1, "delayMs": 1.5}]))]),
            ("Dual EQ", [eq_plugin(eq_preset(
                "dual", left=[bell(900, -2)], right=[bell(1100, -4)]))]),
            ("Same Dual EQ", [eq_plugin(eq_preset(
                "dual", left=[bell(900, -2)], right=[bell(900, -2)]))]),
            ("Loud", [{"id": "loud", "type": "loudness", "params": {}}]),
            ("FIR EQ", [eq_plugin(eq_preset(bands=[bell(500, -1)], eq_mode="FIR"))]),
            ("Alias EQ", [eq_plugin(eq_preset(bands=[
                {"filterType": "pk", "frequencyHz": 700, "gainDb": -2, "q": 1},
                {"filterType": "low_shelf", "frequencyHz": 200, "gainDb": 1, "q": 0.7},
                {"filterType": "high_pass", "frequencyHz": 5000, "gainDb": 0, "q": 0.7}]))]),
            ("Big EQ", [eq_plugin(eq_preset(
                bands=[bell(100 + i * 100, -1) for i in range(20)]))]),
            ("Mid IR", [{"id": "conv", "type": "convolver", "params": {"kernel": "mid"}}]),
            ("Two IR", [{"id": "a", "type": "convolver", "params": {"kernel": "mid"}},
                        {"id": "b", "type": "convolver", "params": {"kernel": "mid"}}]),
        ):
            store.write(name, {"schema": "fxroute.dsp.preset", "version": 1, "chain": chain})
        mono = build_wav_bytes(1, 48000, 32, 3, struct.pack("<8f", *[0.5, 0.25] + [0.0] * 6))
        stereo = build_wav_bytes(2, 48000, 32, 3, struct.pack("<16f", *([0.5, -0.5] + [0.0] * 14)))
        for payload, filename in ((mono, "mid.wav"), (stereo, "wide.wav")):
            source = Path(self.directory.name) / filename
            source.write_bytes(payload)
            self.manager.upload_ir(source, filename)

    def resolve_ir(self, kernel):
        path = self.manager._resolve_kernel_path(kernel)
        params = parse_wav_frames(path)
        ensure_kernel_supported_ir(params, path.name)
        return {"path": str(path), "channels": params["channels"]}

    def compile(self, state):
        return layout_from_plan(state, resolve_ir=self.resolve_ir)

    def crossover_state(self, rate=48000):
        assignments = [f"{side}_{way}" for side in ("left", "right") for way in ("low", "mid", "high")]
        state = switch_mode(set_mode_routing(default_output_state(), "crossover", "A", assignments), "crossover")
        for role, settings in state["modes"]["crossover"]["processing"].items():
            if not role.endswith("low"):
                settings["highpass"] = {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 300 if role.endswith("mid") else 2500}
            if not role.endswith("high"):
                settings["lowpass"] = {"family": "linkwitz-riley", "slope_db_oct": 24, "frequency_hz": 300 if role.endswith("low") else 2500}
        return state

    def stereo_state(self):
        return set_mode_routing(default_output_state(), "stereo", "A", ["main_l", "main_r", "sub1", "sub2"])

    def plan_for(self, state, rate=48000):
        return self._plan_with_channels(state, len(state["modes"][state["active_mode"]]["routing"]["A"]), rate)

    def test_crossover_sos_present_and_bank_edit_selection_irrelevant(self):
        state = self.crossover_state()
        mode = state["modes"]["crossover"]
        mode["banks"]["left_mid"]["preset"] = "Mid EQ"
        mode["banks"]["left_low"]["preset"] = "Mid IR"
        layout = self.compile(self.plan_for(state))
        rows = {row["name"]: row for row in layout}
        self.assertEqual(len(rows["left_low"]["sos"]), 2)
        self.assertEqual(rows["left_low"]["oconv"]["channel"], 0)
        self.assertEqual(len(rows["left_mid"]["filters"]), 1)
        self.assertEqual(rows["left_mid"]["filters"][0]["frequency_hz"], 1000)
        self.assertEqual(len(rows["left_mid"]["sos"]), 4)
        self.assertIsNone(rows["right_mid"]["oconv"])
        self.assertEqual(rows["right_mid"]["filters"], [])
        reselected = select_bank(state, "crossover", "A", 6, "left_mid")
        self.assertEqual(self.compile(self.plan_for(reselected)), layout)

    def test_gain_delay_bands_fold_into_output_trim(self):
        state = self.crossover_state()
        state["modes"]["crossover"]["banks"]["left_mid"]["preset"] = "GainDelay EQ"
        rows = {row["name"]: row for row in self.compile(self.plan_for(state))}
        self.assertEqual(rows["left_mid"]["gain_db"], 2.0)
        self.assertEqual(rows["left_mid"]["delay_ms"], 1.5)
        self.assertEqual([item["type"] for item in rows["left_mid"]["filters"]], ["bell"])

    def test_dual_eq_projects_matching_side(self):
        state = self.crossover_state()
        mode = state["modes"]["crossover"]
        mode["banks"]["left_mid"]["preset"] = "Dual EQ"
        mode["banks"]["right_mid"]["preset"] = "Dual EQ"
        rows = {row["name"]: row for row in self.compile(self.plan_for(state))}
        self.assertEqual(rows["left_mid"]["filters"][0]["frequency_hz"], 900)
        self.assertEqual(rows["right_mid"]["filters"][0]["frequency_hz"], 1100)

    def test_dual_eq_on_mono_sum_requires_identical_sides(self):
        state = self.stereo_state()
        state["modes"]["stereo"]["banks"]["sub1"]["preset"] = "Dual EQ"
        with self.assertRaises(ValueError):
            self.compile(self._plan_with_channels(state, 4))
        state["modes"]["stereo"]["banks"]["sub1"]["preset"] = "Same Dual EQ"
        rows = {row["name"]: row for row in self.compile(self._plan_with_channels(state, 4))}
        self.assertEqual(rows["sub1"]["filters"][0]["frequency_hz"], 900)

    def _plan_with_channels(self, state, channels, rate=48000):
        from dsp.processing_plan import compile_processing_plan
        return compile_processing_plan(state, output_key="A", channels=channels,
                                       sample_rate_hz=rate, preset_loader=self.manager.preset_store.read)

    def test_lone_sub_l_bank_is_mono_not_left_sided(self):
        # A single sub role sums both inputs at 0.5, so its bank is a mono
        # bank even though the role is called sub_l: dual PEQ needs identical
        # sides and a stereo IR is rejected instead of silently using channel
        # 0 only. A real stereo sub pair keeps its per-side behaviour.
        self.manager.preset_store.write("Wide IR", {"schema": "fxroute.dsp.preset", "version": 1,
            "chain": [{"id": "conv", "type": "convolver", "params": {"kernel": "wide"}}]})
        state = self.stereo_state()
        state = set_mode_routing(state, "stereo", "A", ["main_l", "main_r", "sub1"])
        state["modes"]["stereo"]["banks"]["sub1"]["preset"] = "Dual EQ"
        with self.assertRaises(ValueError):
            self.compile(self.plan_for(state))
        state["modes"]["stereo"]["banks"]["sub1"]["preset"] = "Same Dual EQ"
        rows = {row["name"]: row for row in self.compile(self.plan_for(state))}
        self.assertEqual(rows["sub1"]["filters"][0]["frequency_hz"], 900)
        state["modes"]["stereo"]["banks"]["sub1"]["preset"] = "Wide IR"
        with self.assertRaises(ValueError):
            self.compile(self.plan_for(state))

    def test_stereo_sub_pair_bank_keeps_per_side_behaviour(self):
        # With a real sub_l/sub_r pair each side is fed from its own input,
        # so dual PEQ may differ per side and a stereo IR picks its channel.
        state = self.stereo_state()
        state = set_mode_routing(state, "stereo", "A", ["main_l", "main_r", "sub_l", "sub_r"])
        state["modes"]["stereo"]["banks"]["sub_l"]["preset"] = "Dual EQ"
        rows = {row["name"]: row for row in self.compile(self.plan_for(state))}
        self.assertEqual(rows["sub_l"]["filters"][0]["frequency_hz"], 900)
        state["modes"]["stereo"]["banks"]["sub_l"]["preset"] = "Mid IR"
        rows = {row["name"]: row for row in self.compile(self.plan_for(state))}
        self.assertEqual(rows["sub_l"]["oconv"]["channel"], 0)

    def test_stereo_ir_channel_follows_role_side(self):
        state = self.crossover_state()
        mode = state["modes"]["crossover"]
        mode["banks"]["left_mid"]["preset"] = "Mid IR"
        self.manager.preset_store.write("Wide IR", {"schema": "fxroute.dsp.preset", "version": 1,
            "chain": [{"id": "conv", "type": "convolver", "params": {"kernel": "wide"}}]})
        mode["banks"]["right_mid"]["preset"] = "Wide IR"
        rows = {row["name"]: row for row in self.compile(self.plan_for(state))}
        self.assertEqual(rows["left_mid"]["oconv"]["channel"], 0)
        self.assertEqual(rows["right_mid"]["oconv"]["channel"], 1)

    def test_stereo_ir_into_mono_sum_is_rejected(self):
        state = self.stereo_state()
        self.manager.preset_store.write("Wide IR", {"schema": "fxroute.dsp.preset", "version": 1,
            "chain": [{"id": "conv", "type": "convolver", "params": {"kernel": "wide"}}]})
        state["modes"]["stereo"]["banks"]["sub1"]["preset"] = "Wide IR"
        with self.assertRaises(ValueError):
            self.compile(self._plan_with_channels(state, 4))

    def test_direct_bank_keeps_crossover_without_bank_processing(self):
        state = self.crossover_state()
        for bank in state["modes"]["crossover"]["banks"].values():
            bank["preset"] = "Direct"
        rows = {row["name"]: row for row in self.compile(self.plan_for(state))}
        self.assertTrue(rows["left_low"]["sos"])
        self.assertEqual(rows["left_low"]["filters"], [])
        self.assertIsNone(rows["left_low"]["oconv"])

    def test_unsupported_bank_content_fails_loudly(self):
        for preset in ("Loud", "FIR EQ", "Two IR", "Big EQ"):
            state = self.crossover_state()
            state["modes"]["crossover"]["banks"]["left_mid"]["preset"] = preset
            if preset == "Big EQ":
                routing = state["modes"]["crossover"]["routing"]["A"] + ["sub1"]
                state = set_mode_routing(state, "crossover", "A", routing)
                state["modes"]["crossover"]["processing"]["left_mid"]["highpass"] = {
                    "family": "linkwitz-riley", "slope_db_oct": 72, "frequency_hz": 300}
                state["modes"]["crossover"]["processing"]["left_mid"]["lowpass"] = {
                    "family": "linkwitz-riley", "slope_db_oct": 72, "frequency_hz": 2500}
            with self.subTest(preset=preset), self.assertRaises(ValueError):
                self.compile(self.plan_for(state))

    def test_missing_bank_preset_fails_loudly(self):
        state = self.crossover_state()
        state["modes"]["crossover"]["banks"]["left_mid"]["preset"] = "Missing"
        with self.assertRaises(FileNotFoundError):
            self.plan_for(state)

    def test_alias_band_types_map_to_native_names(self):
        state = self.crossover_state()
        state["modes"]["crossover"]["banks"]["left_mid"]["preset"] = "Alias EQ"
        rows = {row["name"]: row for row in self.compile(self.plan_for(state))}
        self.assertEqual([item["type"] for item in rows["left_mid"]["filters"]],
                         ["bell", "lowshelf", "highpass"])

    def test_sos_matches_design_authority_section_for_section(self):
        state = self.crossover_state()
        rows = {row["name"]: row for row in self.compile(self.plan_for(state))}
        expected = design_crossover({"kind": "lowpass", "family": "linkwitz-riley",
                                     "slope_db_oct": 24, "frequency_hz": 300}, 48000)
        self.assertEqual(rows["left_low"]["sos"], [list(section[:3]) + list(section[4:]) for section in expected])


class BankRenderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = DSPManager(home=Path(self.directory.name) / "home")
        ir = build_wav_bytes(1, 48000, 32, 3, struct.pack("<4f", 0.5, 0.25, 0.125, 0.0625))
        source = Path(self.directory.name) / "room.wav"
        source.write_bytes(ir)
        self.manager.upload_ir(source, "room.wav")
        self.ir_path = str(self.manager.irs_dir / "room.wav")

    def layout(self, **overrides):
        entry = {"name": "left_low", "routes": [{"input": 0, "gain": 1.0}],
                 "gain_db": 0.0, "delay_ms": 0.0, "invert": False,
                 "filters": [], "sos": [], "oconv": None}
        entry.update(overrides)
        return [entry]

    def test_sos_and_oconv_render_to_native_lines(self):
        text = self.manager.compile_engine_text(self.layout(
            sos=[[1.0, -1.5, 0.7, -1.4, 0.6]],
            oconv={"path": self.ir_path, "channel": 0, "wet_db": 0.0,
                   "dry_db": -100.0, "input_gain_db": 0.0, "output_gain_db": 0.0},
        ), sample_rate_hz=48000)
        self.assertIn("sos 0 1 -1.5 0.7 -1.4 0.6\n", text)
        self.assertIn(f'oconv 0 0 0 -100 0 0 "{self.ir_path}"\n', text)

    def test_legacy_layout_without_bank_keys_still_renders(self):
        text = self.manager.compile_engine_text(
            [{"name": "FL", "routes": [{"input": 0, "gain": 1.0}]}],
            sample_rate_hz=48000)
        self.assertNotIn("sos ", text)
        self.assertNotIn("oconv ", text)

    def test_unstable_sos_and_budget_overflow_fail(self):
        with self.assertRaises(ValueError):
            self.manager.compile_engine_config(
                self.layout(sos=[[1.0, 0.0, 0.0, 0.0, 1.5]]), sample_rate_hz=48000)
        with self.assertRaises(ValueError):
            self.manager.compile_engine_config(
                self.layout(sos=[[1.0, 0.0, 0.0, 0.0, 0.1]] * 33), sample_rate_hz=48000)

    def test_missing_ir_and_bad_channel_fail(self):
        bad_path = {"path": str(Path(self.directory.name) / "missing.wav"), "channel": 0,
                    "wet_db": 0.0, "dry_db": -100.0, "input_gain_db": 0.0, "output_gain_db": 0.0}
        with self.assertRaises(ValueError):
            self.manager.compile_engine_config(self.layout(oconv=bad_path), sample_rate_hz=48000)
        bad_channel = {**bad_path, "path": self.ir_path, "channel": 3}
        with self.assertRaises(ValueError):
            self.manager.compile_engine_config(self.layout(oconv=bad_channel), sample_rate_hz=48000)


if __name__ == "__main__":
    unittest.main()
