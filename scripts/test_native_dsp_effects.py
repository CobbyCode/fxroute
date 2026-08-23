#!/usr/bin/env python3
import array
import math
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dsp.manager import DSPManager
DSP = ROOT / "native_dsp/build/fxroute-dsp-offline"


def rms(values):
    return math.sqrt(sum(value * value for value in values) / len(values))


def stereo_sine(frequency, left=.1, right=.1, seconds=2):
    return [value for frame in range(int(48000 * seconds))
            for value in (left * math.sin(2 * math.pi * frequency * frame / 48000),
                          right * math.sin(2 * math.pi * frequency * frame / 48000))]


class NativeEffectsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run([str(ROOT / "native_dsp/build.sh")], check=True)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.case_number = 0

    def tearDown(self):
        self.temporary.cleanup()

    def process(self, stages, samples, quantum=127):
        case = self.root / str(self.case_number)
        self.case_number += 1
        case.mkdir()
        config, source, target = case / "dsp.conf", case / "input.f32", case / "output.f32"
        config.write_text(
            "rate 48000\ninputs 2\noutputs 2\n"
            "matrix 0 0 1\nmatrix 1 1 1\n"
            f"{stages}\n"
            "output 0 0 0 normal\noutput 1 0 0 normal\nbypass 0\n"
        )
        with source.open("wb") as handle:
            array.array("f", samples).tofile(handle)
        subprocess.run([str(DSP), str(config), str(source), str(target), str(quantum)], check=True)
        values = array.array("f")
        with target.open("rb") as handle:
            values.fromfile(handle, target.stat().st_size // 4)
        return values

    def process_config(self, config_text, samples, quantum=127):
        case = self.root / str(self.case_number)
        self.case_number += 1
        case.mkdir()
        config, source, target = case / "dsp.conf", case / "input.f32", case / "output.f32"
        config.write_text(config_text)
        with source.open("wb") as handle:
            array.array("f", samples).tofile(handle)
        subprocess.run([str(DSP), str(config), str(source), str(target), str(quantum)], check=True)
        values = array.array("f")
        with target.open("rb") as handle:
            values.fromfile(handle, target.stat().st_size // 4)
        return values

    @staticmethod
    def lv2(identity, uri, controls):
        body = [f"stage_begin 0 {identity} lv2 {uri}"]
        body.extend(f"control {name} {value}" for name, value in controls.items())
        body.append("stage_end")
        return "\n".join(body)

    def test_lsp_peq_applies_independent_left_and_right_bands(self):
        controls = {"mode": 0, "ftl_0": 1, "fml_0": 0,
                    "fl_0": 1000, "gl_0": 3.9810717, "ql_0": 2,
                    "ftr_0": 1, "fmr_0": 0, "fr_0": 1000,
                    "gr_0": .25118864, "qr_0": 2}
        output = self.process(self.lv2("peq", "http://lsp-plug.in/plugins/lv2/para_equalizer_x32_lr", controls),
                              stereo_sine(1000))
        left, right = output[48000::2], output[48001::2]
        self.assertGreater(rms(left), rms(right) * 4)

    def test_lsp_loudness_uses_real_frequency_dependent_contour(self):
        stage = self.lv2("loudness", "http://lsp-plug.in/plugins/lv2/loud_comp_stereo",
                         {"std": 4, "fft": 4,
                          "volume": -40, "hclip": 0, "hcrange": 6})
        gains = {}
        for frequency in (80, 1000, 10000):
            source = stereo_sine(frequency)
            output = self.process(stage, source)
            gains[frequency] = rms(output[48000::2]) / rms(source[48000::2])
        self.assertGreater(gains[80], gains[1000] * 1.5)
        self.assertGreater(gains[10000], gains[1000] * 1.1)

    def test_ebur128_autogain_is_stereo_linked_and_peak_safe(self):
        stage = "\n".join((
            "stage_begin 0 autogain native autogain", "param target_db -18",
            'param reference "Geometric Mean (MSI)"', "param silence_threshold_db -70",
            "param maximum_history_seconds 15", "stage_end"))
        output = self.process(stage, stereo_sine(440, .04, .02, 4), quantum=509)
        left, right = output[-48000::2], output[-47999::2]
        self.assertGreater(rms(left), .04)
        self.assertAlmostEqual(rms(left) / rms(right), 2.0, delta=.03)
        self.assertLess(max(abs(value) for value in output), 1.0)

    def test_lsp_limiter_uses_lookahead_and_stereo_linking(self):
        stage = self.lv2("limiter", "http://lsp-plug.in/plugins/lv2/sc_limiter_stereo",
                         {"enabled": 1, "alr": 0, "mode": 0, "th": .5,
                          "boost": 0, "lk": 5, "at": 5, "rt": 20,
                          "extsc": 0, "slink": 100})
        output = self.process(stage, stereo_sine(440, 1.5, .3), quantum=251)
        left, right = output[48000::2], output[48001::2]
        self.assertLess(max(abs(value) for value in left), .56)
        self.assertLess(rms(right), .18)

    def test_calf_bass_enhancer_and_zamaxim_are_hosted(self):
        bass = self.lv2("bass", "http://calf.sourceforge.net/plugins/BassEnhancer",
                        {"amount": 3.9810717, "drive": 8.5, "freq": 100,
                         "blend": 1, "listen": 0, "floor_active": 0, "floor": 20})
        low = stereo_sine(60)
        bass_output = self.process(bass, low)
        self.assertGreater(rms(bass_output[48000::2]), rms(low[48000::2]) * 1.1)

        maximizer = self.lv2("maximizer", "urn:zamaudio:ZaMaximX2",
                             {"thresh": -6, "rel": 25})
        max_output = self.process(maximizer, stereo_sine(440, .8, .8))
        self.assertTrue(all(math.isfinite(value) for value in max_output))
        self.assertLess(max(abs(value) for value in max_output), 1.05)

    def test_direct_and_neutral_protection_limiter_match_after_latency_and_boost(self):
        # The protection limiter runs with gain-boost enabled like the old
        # pre-native-DSP default: the LSP limiter applies a constant makeup gain
        # equal to |threshold| dB to the output signal (verified against the
        # installed sc_limiter_stereo metadata and the LSP manual).  With
        # threshold -1 dB the Neutral chain therefore differs from the Direct
        # chain by exactly +1.0023 dB after the limiter latency.
        manager = DSPManager(home=self.root / "home")
        manager.save_global_extras({"limiter": {"enabled": True, "params": {
            "thresholdDb": -1, "attackMs": 5, "releaseMs": 50,
            "lookaheadMs": 5, "stereoLinkPercent": 100,
        }}})
        layout = [{"name": "FL", "source": 0}, {"name": "FR", "source": 1}]
        source = stereo_sine(997, .05, .04, 1)
        direct = self.process_config(manager.compile_engine_text(layout, preset_name="Direct"), source)
        neutral = self.process_config(manager.compile_engine_text(layout, preset_name="Neutral"), source)
        direct_left, neutral_left = direct[::2], neutral[::2]
        best = None
        for latency in range(1025):
            count = min(len(direct_left), len(neutral_left) - latency) - 2048
            if count <= 0:
                continue
            gain = rms([neutral_left[latency + 2048 + i] / (direct_left[2048 + i] or 1e-9)
                        for i in range(count)])
            error = rms([neutral_left[latency + 2048 + i] / (direct_left[2048 + i] or 1e-9) - 10.0 ** (1.0 / 20.0)
                         for i in range(count)])
            if best is None or error < best[0]:
                best = (error, latency, count)
        error, latency, count = best
        self.assertLess(error, 2e-4)
        self.assertLessEqual(latency, 1024)


if __name__ == "__main__":
    unittest.main(verbosity=2)
