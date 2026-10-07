#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise LFE bypass and independent Center/LFE effects in the native kernel."""

import array
import math
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPS = ["libebur128", "lilv-0", "samplerate", "speexdsp"]
AVAILABLE = subprocess.run(["pkg-config", "--exists", *DEPS], check=False).returncode == 0


@unittest.skipUnless(AVAILABLE, "missing native DSP build dependencies; build on test host")
class SurroundNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(["sh", str(ROOT / "native_dsp/build.sh")], check=True, capture_output=True)

    def process(self, text, samples, outputs):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "config").write_text(text)
            (path / "input").write_bytes(array.array("f", samples).tobytes())
            subprocess.run([str(ROOT / "native_dsp/build/fxroute-dsp-offline"),
                            str(path / "config"), str(path / "input"), str(path / "output")],
                           check=True, capture_output=True)
            data = array.array("f")
            data.frombytes((path / "output").read_bytes())
            return [data[i::outputs] for i in range(outputs)]

    def test_lfe_bypasses_bass_filter_but_shares_trim_delay_polarity(self):
        text = """rate 1000
inputs 8
surround 1
outputs 2
matrix 0 0 0.5
matrix 1 0 0.5
postmatrix 0 3 1
postmatrix 1 3 1
xsos 0 0.01 0.02 0.01 -1.5 0.54
xsos 1 0.01 0.02 0.01 -1.5 0.54
output 0 -6.020599913 2 invert
output 1 0 0 normal
"""
        frame = [0.0] * 8
        frame[3] = 1.0
        result = self.process(text, frame + [0.0] * (8 * 7), 2)
        self.assertEqual(list(result[0][:2]), [0.0, 0.0])
        self.assertAlmostEqual(result[0][2], -0.5, places=6)
        self.assertEqual(list(result[1]), [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        frame[0], frame[3] = 1.0, 0.0
        bass = self.process(text, frame + [0.0] * (8 * 7), 2)
        self.assertAlmostEqual(bass[1][0], 0.005, places=7)
        self.assertGreater(bass[1][1], 0)

    def test_group_buses_are_low_passed_separately_before_lfe(self):
        text = """rate 1000
inputs 8
surround 1
outputs 1
matrix 0 0 1
gmatrix 0 1 6 1
gmatrix 0 2 4 0.5
postmatrix 0 3 1
xsos 0 0.25 0 0 0 0
gxsos 0 1 0.5 0.5 0 0 0
gxsos 0 2 2 0 0 0 0
output 0 0 0 normal
"""
        def impulse(channel):
            frame = [0.0] * 8
            frame[channel] = 1.0
            return self.process(text, frame + [0.0] * (8 * 3), 1)[0]
        self.assertEqual(list(impulse(0)), [0.25, 0.0, 0.0, 0.0])
        self.assertEqual(list(impulse(6)), [0.5, 0.5, 0.0, 0.0])
        self.assertEqual(list(impulse(4)), [1.0, 0.0, 0.0, 0.0])
        self.assertEqual(list(impulse(3)), [1.0, 0.0, 0.0, 0.0])
        for invalid in ("gmatrix 0 0 6 1\n", "gmatrix 0 4 6 1\n", "gxsos 0 4 1 0 0 0 0\n"):
            with self.subTest(invalid=invalid), self.assertRaises(subprocess.CalledProcessError):
                self.process(text + invalid, [0.0] * 8, 1)

    def test_center_does_not_drive_lfe_limiter_detector(self):
        text = "rate 48000\ninputs 8\nsurround 1\noutputs 8\n"
        text += "".join(f"matrix {i} {i} 1\n" for i in range(8))
        text += """stage_begin 0 limiter lv2 http://lsp-plug.in/plugins/lv2/sc_limiter_stereo
control th 0.1
control boost 0
control slink 100
control dith 0
stage_end
"""
        quiet, loud = [], []
        for i in range(48000):
            frame = [0.0] * 8
            frame[3] = 0.001 * math.sin(2 * math.pi * 80 * i / 48000)
            quiet.extend(frame)
            frame[2] = 0.5 * math.sin(2 * math.pi * 1000 * i / 48000)
            loud.extend(frame)
        self.assertEqual(self.process(text, quiet, 8)[3], self.process(text, loud, 8)[3])
        linked = text.replace("surround 1\n", "")
        self.assertNotEqual(self.process(linked, quiet, 8)[3], self.process(linked, loud, 8)[3])

    def test_unlinked_surround_positions_are_zero_without_silencing_center(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / "config"
            config.write_text("rate 48000\ninputs 8\nsurround 1\noutputs 2\nmatrix 0 2 1\nmatrix 1 3 1\n")
            source = path / "null_inputs.c"
            source.write_text('''#include "dsp.h"
#include <stdio.h>
int main(int argc, char **argv) {
    char error[256];
    (void)argc;
    fxdsp *d = fxdsp_load(argv[1], error, sizeof error);
    if (!d) { fprintf(stderr, "%s", error); return 2; }
    float silence[4] = {0}, center[4] = {0.25f, -0.5f, 0, 1}, out[2][4] = {{0}};
    const float *input[8] = {silence, silence, center, NULL, NULL, NULL, NULL, NULL};
    float *output[2] = {out[0], out[1]};
    fxdsp_process(d, input, output, 4);
    for (unsigned i = 0; i < 4; i++) if (out[0][i] != center[i] || out[1][i] != 0) return 3;
    fxdsp_free(d);
    return 0;
}
''')
            native = ROOT / "native_dsp"
            flags = subprocess.check_output(["pkg-config", "--cflags", "--libs", *DEPS], text=True).split()
            subprocess.run(["cc", "-std=c11", "-O2", "-I", str(native), str(source),
                *(str(native / file) for file in ("dsp.c", "worker_pool.c", "timing.c", "autogain.c", "crystalizer.c", "upmix_fast.c", "upmix_hq.c", "lv2_host.c")),
                *flags, "-lm", "-pthread", "-o", str(path / "test")], check=True, capture_output=True)
            subprocess.run([str(path / "test"), str(config)], check=True, capture_output=True)


    def convolver_text(self, directory, pairs=None):
        ir = Path(directory) / "half.wav"
        with wave.open(str(ir), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(array.array("h", [16384, 0, 0, 0]).tobytes())
        text = "rate 48000\ninputs 8\nsurround 1\noutputs 8\n"
        text += "".join(f"matrix {i} {i} 1\n" for i in range(8))
        text += f'stage_begin 0 conv native convolver\nparam path "{ir}"\nparam wet_db 0\nparam dry_db -100\n'
        if pairs is not None:
            text += f"pairs {pairs}\n"
        return text + "stage_end\n"

    def test_convolver_pair_mask_leaves_other_pairs_untouched(self):
        frames = 2048
        samples = []
        for frame in range(frames):
            row = [0.0] * 8
            if frame == 10:
                row[0], row[2], row[4] = 1.0, 0.8, 0.6
            samples.extend(row)
        with tempfile.TemporaryDirectory() as directory:
            plain = self.process(self.convolver_text(directory), samples, 8)
            center_only = self.process(self.convolver_text(directory, pairs=2), samples, 8)
        self.assertAlmostEqual(sum(plain[2]), 0.4, places=3)
        self.assertAlmostEqual(sum(plain[0]), 0.5, places=3)
        # Pair 1 (FC) convolves; FL/FR and SL/SR pass through unchanged, not
        # attenuated by the convolver's -100 dB dry path.
        self.assertAlmostEqual(sum(center_only[2]), 0.4, places=3)
        self.assertEqual(sum(center_only[0]), 1.0)
        self.assertEqual(sum(center_only[4]), 0.6000000238418579)
        self.assertEqual(list(center_only[0]), [1.0 if frame == 10 else 0.0 for frame in range(frames)])

    def test_pair_mask_masks_lv2_stages_and_passes_other_pairs_through(self):
        text = "rate 48000\ninputs 8\nsurround 1\noutputs 8\n"
        text += "".join(f"matrix {i} {i} 1\n" for i in range(8))
        limiter = """stage_begin 0 limiter lv2 http://lsp-plug.in/plugins/lv2/sc_limiter_stereo
control th 0.1
control boost 0
control slink 100
control dith 0
{pairs}stage_end
"""
        loud = []
        for i in range(24000):
            frame = [0.0] * 8
            frame[2] = 0.5 * math.sin(2 * math.pi * 1000 * i / 48000)
            frame[4] = 0.5 * math.sin(2 * math.pi * 700 * i / 48000)
            loud.extend(frame)
        loud = array.array("f", loud).tolist()  # the engine reads float32
        center = [loud[8 * i + 2] for i in range(24000)]
        side = [loud[8 * i + 4] for i in range(24000)]
        front_only = self.process(text + limiter.format(pairs="pairs 1\n"), loud, 8)
        self.assertTrue(list(front_only[2]) == center, "pass-through of an unmasked pair must be bit exact")
        self.assertTrue(list(front_only[4]) == side, "pass-through of an unmasked pair must be bit exact")
        center_only = self.process(text + limiter.format(pairs="pairs 2\n"), loud, 8)
        self.assertFalse(list(center_only[2]) == center, "the masked pair must be limited")
        self.assertTrue(list(center_only[4]) == side, "the unmasked pair must stay untouched")

    def test_pair_mask_is_rejected_outside_its_contract(self):
        base = "rate 48000\ninputs 8\nsurround 1\noutputs 8\n" + "".join(f"matrix {i} {i} 1\n" for i in range(8))
        with tempfile.TemporaryDirectory() as directory:
            conv = self.convolver_text(directory)
            for label, text in (
                ("no pair", conv.replace("stage_end", "pairs 0\nstage_end")),
                ("unknown pair", conv.replace("stage_end", "pairs 32\nstage_end")),
                ("not surround", conv.replace("surround 1\n", "").replace("stage_end", "pairs 1\nstage_end")),
                ("wrong stage", base + "stage_begin 0 gain native headroom\nparam gain_db 0\npairs 1\nstage_end\n"),
            ):
                with self.subTest(label), self.assertRaises(subprocess.CalledProcessError):
                    self.process(text, [0.0] * 8, 8)

    def test_input_mute_zeroes_only_the_masked_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / "config"
            config.write_text("rate 48000\ninputs 8\nsurround 1\noutputs 8\n"
                              + "".join(f"matrix {i} {i} 1\n" for i in range(8)))
            source = path / "input_mute.c"
            source.write_text('''#include "dsp.h"
#include <stdio.h>
static int check(fxdsp *d, uint32_t muted) {
    float in[8][4], out[8][4];
    const float *input[8]; float *output[8];
    for (unsigned c = 0; c < 8; c++) {
        for (unsigned n = 0; n < 4; n++) in[c][n] = 0.1f * (float)(c + 1) + (float)n;
        input[c] = in[c]; output[c] = out[c];
    }
    fxdsp_process(d, input, output, 4);
    for (unsigned c = 0; c < 8; c++)
        for (unsigned n = 0; n < 4; n++) {
            float want = (muted >> c) & 1U ? 0.0f : in[c][n];
            if (out[c][n] != want) return 1;
        }
    return 0;
}
int main(int argc, char **argv) {
    char error[256];
    (void)argc;
    fxdsp *d = fxdsp_load(argv[1], error, sizeof error);
    if (!d) { fprintf(stderr, "%s", error); return 2; }
    if (check(d, 0)) return 3;
    fxdsp_set_input_mute(d, 0x3U, 1);
    if (check(d, 0x3U)) return 4;
    fxdsp_set_input_mute(d, 0x4U, 1);
    if (check(d, 0x7U)) return 5;
    fxdsp_set_input_mute(d, 0x3U, 0);
    if (check(d, 0x4U)) return 6;
    fxdsp_set_input_mute(d, 0x4U, 0);
    if (check(d, 0)) return 7;
    fxdsp_free(d);
    return 0;
}
''')
            native = ROOT / "native_dsp"
            flags = subprocess.check_output(["pkg-config", "--cflags", "--libs", *DEPS], text=True).split()
            subprocess.run(["cc", "-std=c11", "-O2", "-I", str(native), str(source),
                *(str(native / file) for file in ("dsp.c", "worker_pool.c", "timing.c", "autogain.c", "crystalizer.c", "upmix_fast.c", "upmix_hq.c", "lv2_host.c")),
                *flags, "-lm", "-pthread", "-o", str(path / "test")], check=True, capture_output=True)
            subprocess.run([str(path / "test"), str(config)], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
