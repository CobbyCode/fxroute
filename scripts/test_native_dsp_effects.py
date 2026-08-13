#!/usr/bin/env python3
import array
import math
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DSP = ROOT / "native_dsp/build/fxroute-dsp-offline"


def rms(values):
    return math.sqrt(sum(value * value for value in values) / len(values))


def sine(frequency, amplitude=.1, seconds=1):
    return [amplitude * math.sin(2 * math.pi * frequency * frame / 48000)
            for frame in range(int(48000 * seconds))]


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

    def process(self, command, samples, outputs=1, quantum=127, check=True):
        case = self.root / str(self.case_number); self.case_number += 1; case.mkdir()
        routes = "\n".join(f"matrix {output} 0 1" for output in range(outputs))
        config, source, target = case / "dsp.conf", case / "input.f32", case / "output.f32"
        config.write_text(f"rate 48000\ninputs 1\noutputs {outputs}\n{routes}\n{command}\n")
        with source.open("wb") as handle:
            array.array("f", samples).tofile(handle)
        result = subprocess.run([str(DSP), str(config), str(source), str(target), str(quantum)],
                                text=True, capture_output=True, check=check)
        values = array.array("f")
        if target.exists():
            with target.open("rb") as handle:
                values.fromfile(handle, target.stat().st_size // 4)
        return values, result

    def test_autogain_and_silence(self):
        normalized, _ = self.process("autogain -12 -50 .25", [.1] * 96000)
        self.assertTrue(.20 < rms(normalized[-24000:]) < .28)
        silent, _ = self.process("autogain -12 -40 1", [.001] * 24000)
        self.assertLess(max(abs(value - .001) for value in silent), 1e-6)

    def test_loudness_frequency_and_strength(self):
        gains = {}
        for frequency in (80, 1000, 10000):
            baseline, _ = self.process("", sine(frequency))
            effected, _ = self.process("loudness -50 10", sine(frequency))
            gains[frequency] = rms(effected[12000:]) / rms(baseline[12000:])
        self.assertGreater(gains[80], gains[1000] * 1.5)
        self.assertGreater(gains[10000], gains[1000] * 1.15)

    def test_bass_enhancer_scope(self):
        low, high = sine(60, .15), sine(1000, .15)
        low_wet, _ = self.process("bass_enhancer 12 8.5 120 100", low)
        high_wet, _ = self.process("bass_enhancer 12 8.5 120 100", high)
        self.assertGreater(rms(low_wet[12000:]), rms(low[12000:]) * 1.2)
        self.assertLess(abs(rms(high_wet[12000:]) - rms(high[12000:])), .02)

    def test_crystalizer_and_maximizer(self):
        impulses = [0.] * 48000
        for frame in range(0, len(impulses), 2400): impulses[frame] = .5
        enhanced, _ = self.process("crystalizer", impulses)
        self.assertGreater(max(enhanced), .55)
        maximized, _ = self.process("maximizer -1", sine(440, 2))
        self.assertLessEqual(max(abs(value) for value in maximized), 10 ** (-1 / 20) + 1e-6)

    def test_arbitrary_outputs_bypass_and_strict_commands(self):
        samples = sine(80, .2, .25)
        commands = "autogain -18 -60 1\nloudness -30 6\nbass_enhancer 6 8 100 50\ncrystalizer\nmaximizer -1"
        effected, _ = self.process(commands, samples, outputs=5)
        self.assertEqual(len(effected), len(samples) * 5)
        bypassed, _ = self.process(commands + "\nbypass 1", samples, outputs=5)
        for frame, source in enumerate(samples): self.assertAlmostEqual(bypassed[frame * 5], source, places=6)
        for command in ("autogain -12 -50", "loudness -50 0", "crystalizer 1", "maximizer 1"):
            _, result = self.process(command, [0.], check=False)
            self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
